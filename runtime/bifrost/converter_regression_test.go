package bifrost

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

func TestAnthropicRestorationFollowsGroupedToolResults(t *testing.T) {
	for _, extensions := range []bool{false, true} {
		var body map[string]any
		_ = json.Unmarshal([]byte(`{"model":"claude-sonnet-4-6","max_tokens":64,"messages":[{"role":"user","content":"question"},{"role":"assistant","content":null,"tool_calls":[{"id":"call_1","type":"function","function":{"name":"first","arguments":"{}"}},{"id":"call_2","type":"function","function":{"name":"second","arguments":"{}"}}]},{"role":"tool","tool_call_id":"call_1","content":"first result"},{"role":"tool","tool_call_id":"call_2","content":"second result"},{"role":"assistant","content":"following assistant"}]}`), &body)
		if extensions {
			messages := body["messages"].([]any)
			messages[1].(map[string]any)["provider_specific_fields"] = map[string]any{"compaction_blocks": []any{map[string]any{"type": "compaction", "content": "first head"}}}
			messages[4].(map[string]any)["provider_specific_fields"] = map[string]any{"compaction_blocks": []any{map[string]any{"type": "compaction", "content": "following head"}}}
		}
		raw, _ := json.Marshal(body)
		plan, err := prepare(context.Background(), Envelope{Protocol: "chat", Provider: "anthropic", APIKey: "selected", Body: raw})
		if err != nil {
			t.Fatalf("extensions=%v: %v", extensions, err)
		}
		var rendered struct {
			Messages []struct {
				Role    string          `json:"role"`
				Content json.RawMessage `json:"content"`
			} `json:"messages"`
		}
		if json.Unmarshal(plan.body, &rendered) != nil || len(rendered.Messages) != 4 {
			t.Fatalf("grouped continuation lost: %s", plan.body)
		}
		var results []map[string]any
		if json.Unmarshal(rendered.Messages[2].Content, &results) != nil || len(results) != 2 || results[0]["tool_use_id"] != "call_1" || results[1]["tool_use_id"] != "call_2" {
			t.Fatalf("tool results corrupted: %s", plan.body)
		}
		if extensions {
			var first, last []map[string]any
			_ = json.Unmarshal(rendered.Messages[1].Content, &first)
			_ = json.Unmarshal(rendered.Messages[3].Content, &last)
			if len(first) < 3 || len(last) < 2 || first[0]["content"] != "first head" || last[0]["content"] != "following head" {
				t.Fatalf("extension patched wrong assistant: %s", plan.body)
			}
		}
		plan.close()
	}
}

func TestResponsesParallelToolIndexesWithAndWithoutReasoning(t *testing.T) {
	for _, reasoning := range []bool{false, true} {
		upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/event-stream")
			emit := func(event any) { raw, _ := json.Marshal(event); _, _ = io.WriteString(w, "data: "+string(raw)+"\n\n") }
			start := 0
			if reasoning {
				emit(map[string]any{"type": "response.output_item.added", "output_index": 0, "item": map[string]any{"type": "reasoning", "id": "reason_1"}})
				start = 1
			}
			for i, name := range []string{"first", "second"} {
				emit(map[string]any{"type": "response.output_item.added", "output_index": start + i, "item": map[string]any{"type": "function_call", "id": "item_" + name, "call_id": "call_" + name, "name": name, "arguments": ""}})
			}
			emit(map[string]any{"type": "response.function_call_arguments.delta", "output_index": start, "item_id": "item_first", "delta": "{"})
			emit(map[string]any{"type": "response.function_call_arguments.delta", "output_index": start + 1, "item_id": "item_second", "delta": "{\"x\":"})
			emit(map[string]any{"type": "response.function_call_arguments.delta", "output_index": start, "item_id": "item_first", "delta": "}"})
			emit(map[string]any{"type": "response.function_call_arguments.delta", "output_index": start + 1, "item_id": "item_second", "delta": "1}"})
			emit(map[string]any{"type": "response.completed", "response": map[string]any{"id": "resp_1", "status": "completed", "model": "gpt-4.1-mini", "output": []any{}, "usage": map[string]any{"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}}})
		}))
		handler, server := runtimeFixture(t)
		target, _ := url.Parse(upstream.URL)
		handler.client.Transport = rewriteTransport{target: target, inner: handler.client.Transport}
		e := chatEnvelope("")
		e.Body = json.RawMessage(`{"model":"gpt-4.1-mini","messages":[{"role":"user","content":"tools"}],"stream":true}`)
		status, result := execute(t, server, e)
		ids := make(map[int]string)
		arguments := make(map[int]string)
		for _, line := range strings.Split(string(result), "\n") {
			if !strings.HasPrefix(line, "data: ") || line == "data: [DONE]" {
				continue
			}
			var chunk struct {
				Choices []struct {
					Delta struct {
						ToolCalls []struct {
							Index    int    `json:"index"`
							ID       string `json:"id"`
							Function struct {
								Arguments string `json:"arguments"`
							} `json:"function"`
						} `json:"tool_calls"`
					} `json:"delta"`
				} `json:"choices"`
			}
			if json.Unmarshal([]byte(strings.TrimPrefix(line, "data: ")), &chunk) != nil {
				t.Fatalf("bad chat event %s", line)
			}
			for _, choice := range chunk.Choices {
				for _, call := range choice.Delta.ToolCalls {
					if call.ID != "" {
						ids[call.Index] = call.ID
					}
					arguments[call.Index] += call.Function.Arguments
				}
			}
		}
		if status != 200 || ids[0] != "call_first" || ids[1] != "call_second" || len(ids) != 2 || arguments[0] != "{}" || arguments[1] != "{\"x\":1}" || !bytes.Contains(result, []byte("[DONE]")) {
			t.Fatalf("reasoning=%v indexes=%v arguments=%v response=%s", reasoning, ids, arguments, result)
		}
		upstream.Close()
	}
}
