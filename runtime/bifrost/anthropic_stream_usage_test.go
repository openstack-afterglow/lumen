package bifrost

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// The pinned Anthropic chat stream converter drops message_start usage and the
// message_delta stop reason and usage. Provider-reported accounting must still
// reach Lumen on every protocol; a local estimate would silently replace it.

func anthropicStream(start, delta string, withStop bool) string {
	stream := "data: {\"type\":\"message_start\",\"message\":{\"id\":\"m\",\"type\":\"message\",\"role\":\"assistant\",\"model\":\"claude-x\",\"content\":[]" + start + "}}\n\n" +
		"data: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n" +
		"data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"hello\"}}\n\n" +
		"data: {\"type\":\"content_block_stop\",\"index\":0}\n\n" +
		"data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"end_turn\",\"stop_sequence\":null}" + delta + "}\n\n"
	if withStop {
		stream += "data: {\"type\":\"message_stop\"}\n\n"
	}
	return stream
}

func runAnthropicStream(t *testing.T, protocol, stream string) (int, string) {
	t.Helper()
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, stream)
	}))
	defer upstream.Close()
	_, server := runtimeFixture(t)
	body := `{"model":"claude-x","messages":[{"role":"user","content":"hi"}],"max_tokens":16,"stream":true}`
	switch protocol {
	case "responses":
		body = `{"model":"claude-x","input":"hi","max_output_tokens":16,"stream":true}`
	case "messages":
		body = `{"model":"claude-x","messages":[{"role":"user","content":"hi"}],"max_tokens":16,"stream":true}`
	}
	// Native Anthropic messages are passed through untouched; chat and Responses
	// run through the converter, which is where the dropped accounting lived.
	raw, _ := json.Marshal(Envelope{Protocol: protocol, Provider: "anthropic", APIBase: upstream.URL, APIKey: "k", Body: json.RawMessage(body)})
	req, _ := http.NewRequestWithContext(context.Background(), http.MethodPost, server.URL+"/v1/execute", bytes.NewReader(raw))
	req.Header.Set("Authorization", "Bearer private-test-token")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	out, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(out)
}

func frames(output string) []map[string]any {
	var result []map[string]any
	for _, frame := range strings.Split(output, "\n\n") {
		for _, line := range strings.Split(frame, "\n") {
			payload, ok := strings.CutPrefix(line, "data: ")
			if !ok || payload == "[DONE]" {
				continue
			}
			var event map[string]any
			if json.Unmarshal([]byte(payload), &event) == nil {
				result = append(result, event)
			}
		}
	}
	return result
}

func number(value any) (int, bool) {
	f, ok := value.(float64)
	return int(f), ok
}

// chatUsage returns the last normalized usage object seen on a chat stream.
func chatUsage(output string) (usage map[string]any, finish string) {
	for _, event := range frames(output) {
		if u, ok := event["usage"].(map[string]any); ok {
			usage = u
		}
		if choices, ok := event["choices"].([]any); ok && len(choices) > 0 {
			if choice, ok := choices[0].(map[string]any); ok {
				if reason, ok := choice["finish_reason"].(string); ok {
					finish = reason
				}
			}
		}
	}
	return usage, finish
}

func TestAnthropicChatStreamCarriesProviderUsageAndFinishReason(t *testing.T) {
	cases := []struct {
		name, start, delta string
		prompt, completion int
	}{
		{"plain", `,"usage":{"input_tokens":10,"output_tokens":0}`, `,"usage":{"output_tokens":5}`, 10, 5},
		{"cache reads and writes count as prompt input",
			`,"usage":{"input_tokens":100,"cache_read_input_tokens":150,"cache_creation_input_tokens":30,"output_tokens":1}`,
			`,"usage":{"output_tokens":45}`, 280, 45},
		{"a later report replaces a counter without erasing omitted ones",
			`,"usage":{"input_tokens":10,"cache_read_input_tokens":7,"output_tokens":0}`,
			`,"usage":{"input_tokens":12,"output_tokens":9}`, 19, 9},
		{"compaction iterations are additive to the final message",
			`,"usage":{"input_tokens":10,"output_tokens":0}`,
			`,"usage":{"input_tokens":10,"output_tokens":5,"cache_read_input_tokens":100,"cache_creation_input_tokens":30,"iterations":[` +
				`{"type":"compaction","input_tokens":200,"output_tokens":40,"cache_read_input_tokens":50,"cache_creation_input_tokens":0},` +
				`{"type":"message","input_tokens":10,"output_tokens":5,"cache_read_input_tokens":100,"cache_creation_input_tokens":30}]}`,
			390, 45},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			status, out := runAnthropicStream(t, "chat", anthropicStream(tc.start, tc.delta, true))
			usage, finish := chatUsage(out)
			if status != 200 || finish != "stop" {
				t.Fatalf("status=%d finish=%q out=%s", status, finish, out)
			}
			prompt, _ := number(usage["prompt_tokens"])
			completion, _ := number(usage["completion_tokens"])
			total, _ := number(usage["total_tokens"])
			if usage == nil || prompt != tc.prompt || completion != tc.completion || total != tc.prompt+tc.completion {
				t.Fatalf("provider usage lost or invented: %v in %s", usage, out)
			}
			if !strings.Contains(out, `"provider_usage"`) {
				t.Fatalf("raw provider usage not preserved for cache accounting: %s", out)
			}
			if strings.Count(out, "data: [DONE]") != 1 {
				t.Fatalf("terminal sentinel count wrong: %s", out)
			}
		})
	}
}

func TestAnthropicChatStreamKeepsCacheWriteTTLSplit(t *testing.T) {
	_, out := runAnthropicStream(t, "chat", anthropicStream(
		`,"usage":{"input_tokens":10,"output_tokens":0}`,
		`,"usage":{"output_tokens":5,"cache_creation_input_tokens":30,"cache_creation":{"ephemeral_5m_input_tokens":20,"ephemeral_1h_input_tokens":10}}`, true))
	usage, _ := chatUsage(out)
	details, _ := usage["prompt_tokens_details"].(map[string]any)
	split, _ := details["cached_write_token_details"].(map[string]any)
	five, _ := number(split["cached_write_tokens_5m"])
	hour, _ := number(split["cached_write_tokens_1h"])
	if five != 20 || hour != 10 {
		t.Fatalf("cache write TTL split lost: %v in %s", usage, out)
	}
}

// A stream that reports no usage must not gain an invented zero, and the other
// direction must not be fabricated when only one is reported.
func TestAnthropicChatStreamLeavesUnreportedUsageUnknown(t *testing.T) {
	_, out := runAnthropicStream(t, "chat", anthropicStream("", "", true))
	usage, finish := chatUsage(out)
	if usage != nil || strings.Contains(out, "provider_usage") {
		t.Fatalf("usage invented for a stream that reported none: %s", out)
	}
	if finish != "stop" {
		t.Fatalf("finish reason lost: %s", out)
	}
}

// Truncation between message_delta and message_stop must not look complete.
func TestAnthropicStreamWithoutMessageStopEmitsNoTerminal(t *testing.T) {
	for _, protocol := range []string{"chat", "responses"} {
		t.Run(protocol, func(t *testing.T) {
			_, out := runAnthropicStream(t, protocol, anthropicStream(`,"usage":{"input_tokens":10,"output_tokens":0}`, `,"usage":{"output_tokens":5}`, false))
			if strings.Contains(out, "[DONE]") || strings.Contains(out, "response.completed") {
				t.Fatalf("truncated stream manufactured a terminal: %s", out)
			}
			if usage, _ := chatUsage(out); usage != nil {
				t.Fatalf("usage emitted before the message completed: %s", out)
			}
		})
	}
}

// /v1/responses routed to a Claude model used to end without response.completed
// at all, so every successful request landed in the unknown-outcome path.
func TestAnthropicResponsesStreamCompletesWithProviderUsage(t *testing.T) {
	status, out := runAnthropicStream(t, "responses", anthropicStream(
		`,"usage":{"input_tokens":10,"cache_read_input_tokens":7,"output_tokens":0}`, `,"usage":{"output_tokens":5}`, true))
	if status != 200 {
		t.Fatalf("status=%d %s", status, out)
	}
	var completed map[string]any
	for _, event := range frames(out) {
		if event["type"] == "response.completed" {
			completed = event
		}
	}
	if completed == nil {
		t.Fatalf("no response.completed for an Anthropic stream that finished: %s", out)
	}
	response, _ := completed["response"].(map[string]any)
	usage, _ := response["usage"].(map[string]any)
	input, _ := number(usage["input_tokens"])
	output, _ := number(usage["output_tokens"])
	if input != 17 || output != 5 {
		t.Fatalf("Responses usage lost or wrong (want input 10+7 cache read, output 5): %v in %s", usage, out)
	}
	if status, _ := response["status"].(string); status != "completed" {
		t.Fatalf("status %q: %s", status, out)
	}
	if strings.Contains(out, "[DONE]") {
		t.Fatalf("native protocol received an invented chat sentinel: %s", out)
	}
}
