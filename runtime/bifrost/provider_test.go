package bifrost

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

func TestCohereV2AdapterUsesRealSingleHTTPAttemptAndUsage(t *testing.T) {
	var calls atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/v2/chat" {
			t.Error(r.URL.Path)
		}
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		if body["max_tokens"] != float64(31) || body["tools"] == nil {
			t.Error("Cohere request lost budget or tools")
		}
		_, _ = w.Write([]byte(`{"id":"cohere-1","finish_reason":"COMPLETE","message":{"role":"assistant","content":[{"type":"text","text":"cohere result"}]},"usage":{"billed_units":{"input_tokens":5,"output_tokens":2},"tokens":{"input_tokens":5,"output_tokens":2}}}`))
	}))
	defer upstream.Close()
	_, server := runtimeFixture(t)
	status, result := execute(t, server, Envelope{Provider: "cohere", Protocol: "chat", APIBase: upstream.URL, APIKey: "cohere-A", Body: json.RawMessage(`{"model":"command-r","messages":[{"role":"user","content":"hi"}],"max_tokens":31,"tools":[{"type":"function","function":{"name":"lookup","parameters":{"type":"object","properties":{}}}}]}`)})
	if status != 200 || calls.Load() != 1 || !bytes.Contains(result, []byte("cohere result")) || !bytes.Contains(result, []byte("prompt_tokens")) {
		t.Fatalf("%d calls=%d %s", status, calls.Load(), result)
	}
}
func TestCohereTerminalIsRequiredForChatSentinel(t *testing.T) {
	for _, complete := range []bool{false, true} {
		upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/event-stream")
			_, _ = io.WriteString(w, "data: "+`{"type":"content-delta","index":0,"delta":{"message":{"content":{"text":"hello"}}}}`+"\n\n")
			if complete {
				_, _ = io.WriteString(w, "data: "+`{"type":"message-end","delta":{"finish_reason":"COMPLETE","usage":{"billed_units":{"input_tokens":2,"output_tokens":1},"tokens":{"input_tokens":2,"output_tokens":1}}}}`+"\n\n")
			}
		}))
		_, server := runtimeFixture(t)
		status, result := execute(t, server, Envelope{Provider: "cohere", Protocol: "chat", APIBase: upstream.URL, APIKey: "a", Body: json.RawMessage(`{"model":"command-r","messages":[{"role":"user","content":"hi"}],"stream":true}`)})
		if status != 200 || bytes.Contains(result, []byte("[DONE]")) != complete {
			t.Fatalf("terminal=%v status=%d %s", complete, status, result)
		}
		upstream.Close()
	}
}
func TestAdvertisedOpenAICompatibleProvidersRemainUsable(t *testing.T) {
	for _, provider := range []string{"mistral", "groq", "together_ai", "together"} {
		plan, err := prepare(context.Background(), Envelope{Provider: provider, Protocol: "chat", APIKey: "selected", Body: json.RawMessage(`{"model":"model","messages":[{"role":"user","content":"hi"}],"reasoning_effort":"none","max_tokens":11}`)})
		if err != nil {
			t.Fatalf("%s %v", provider, err)
		}
		if !strings.HasSuffix(plan.target, "/chat/completions") || !bytes.Contains(plan.body, []byte(`"reasoning_effort":"none"`)) {
			t.Fatalf("%s wrong request %s %s", provider, plan.target, plan.body)
		}
		plan.close()
	}
}
func TestGeminiEmbeddingAndModalityUsageUseUpstreamConverters(t *testing.T) {
	var calls atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("X-Goog-Api-Key") != "gemini-A" || r.Header.Get("Authorization") != "" {
			t.Error("Gemini credential mixed")
		}
		if strings.HasSuffix(r.URL.Path, ":batchEmbedContents") {
			_, _ = io.WriteString(w, `{"embeddings":[{"values":[0.1,0.2,0.3]}]}`)
			return
		}
		_, _ = io.WriteString(w, `{"responseId":"g1","modelVersion":"gemini-2.5-flash","candidates":[{"content":{"role":"model","parts":[{"text":"hello"}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":15,"candidatesTokenCount":3,"totalTokenCount":18,"promptTokensDetails":[{"modality":"TEXT","tokenCount":5},{"modality":"IMAGE","tokenCount":10}],"cacheTokensDetails":[{"modality":"IMAGE","tokenCount":2}],"cachedContentTokenCount":2}}`)
	}))
	defer upstream.Close()
	_, server := runtimeFixture(t)
	status, result := execute(t, server, Envelope{Provider: "gemini", Protocol: "embeddings", APIBase: upstream.URL + "/v1beta", APIKey: "gemini-A", Body: json.RawMessage(`{"model":"text-embedding-004","input":["hello"],"dimensions":3}`)})
	if status != 200 || !bytes.Contains(result, []byte("0.1")) {
		t.Fatalf("embedding %d %s", status, result)
	}
	status, result = execute(t, server, Envelope{Provider: "gemini", Protocol: "chat", APIBase: upstream.URL + "/v1beta", APIKey: "gemini-A", Body: json.RawMessage(`{"model":"gemini-2.5-flash","messages":[{"role":"user","content":"hello"}],"max_tokens":10}`)})
	if status != 200 || !bytes.Contains(result, []byte("promptTokensDetails")) || !bytes.Contains(result, []byte("IMAGE")) || calls.Load() != 2 {
		t.Fatalf("modality evidence %d %s calls=%d", status, result, calls.Load())
	}
}
func TestNativeGeminiGenerateKeepsWireAndRequiresUsageTerminal(t *testing.T) {
	for _, complete := range []bool{false, true} {
		var paths []string
		upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			paths = append(paths, r.URL.RequestURI())
			body, _ := io.ReadAll(r.Body)
			if r.Header.Get("X-Goog-Api-Key") != "gemini-A" || !bytes.Equal(body, []byte(`{"contents":[{"parts":[{"text":"hi"}]}],"safetySettings":[{"category":"X","threshold":"BLOCK_NONE"}]}`)) {
				t.Errorf("native request changed: %s", body)
			}
			if !strings.Contains(r.URL.RawQuery, "alt=sse") {
				_, _ = io.WriteString(w, `{"candidates":[{"content":{"parts":[{"thoughtSignature":"sig","text":"ok"}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":2,"candidatesTokenCount":1}}`)
				return
			}
			w.Header().Set("Content-Type", "text/event-stream")
			_, _ = io.WriteString(w, "data: "+`{"candidates":[{"content":{"parts":[{"text":"o"}]},"finishReason":"STOP"}]}`+"\n\n")
			if complete {
				_, _ = io.WriteString(w, "data: "+`{"usageMetadata":{"promptTokenCount":2,"candidatesTokenCount":1}}`+"\n\n")
			}
		}))
		_, server := runtimeFixture(t)
		request := `{"contents":[{"parts":[{"text":"hi"}]}],"safetySettings":[{"category":"X","threshold":"BLOCK_NONE"}]}`
		status, result := execute(t, server, Envelope{Provider: "gemini", Protocol: "gemini_generate", APIBase: upstream.URL + "/v1beta", APIKey: "gemini-A", Body: json.RawMessage(`{"model":"gemini-2.5-flash","stream":false,"request":` + request + `}`)})
		if status != 200 || !bytes.Contains(result, []byte(`"thoughtSignature":"sig"`)) {
			t.Fatalf("native response changed %d %s", status, result)
		}
		status, result = execute(t, server, Envelope{Provider: "gemini", Protocol: "gemini_generate", APIBase: upstream.URL + "/v1beta", APIKey: "gemini-A", Body: json.RawMessage(`{"model":"gemini-2.5-flash","stream":true,"request":` + request + `}`)})
		if status != 200 || bytes.Contains(result, []byte("[DONE]")) != complete {
			t.Fatalf("usage terminal=%v %d %s", complete, status, result)
		}
		if paths[0] != "/v1beta/models/gemini-2.5-flash:generateContent" || paths[1] != "/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse" {
			t.Fatalf("wrong targets %v", paths)
		}
		upstream.Close()
	}
	if _, err := prepare(context.Background(), Envelope{Provider: "openai", Protocol: "gemini_generate", APIKey: "a", Body: json.RawMessage(`{"model":"m","request":{}}`)}); err == nil {
		t.Fatal("non-Gemini provider accepted the native Gemini protocol")
	}
}
func TestNativeResponsesEventsRetainReasoningAndToolPayload(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		for _, event := range []string{
			`{"type":"response.output_item.added","output_index":0,"item":{"type":"reasoning","encrypted_content":"opaque-signed-payload","future":true}}`,
			`{"type":"response.output_item.added","output_index":1,"item":{"type":"function_call","call_id":"call_1","name":"lookup","arguments":"{}"}}`,
			`{"type":"response.completed","response":{"id":"r1","status":"completed","usage":{"input_tokens":2,"output_tokens":1}}}`,
		} {
			_, _ = io.WriteString(w, "data: "+event+"\n\n")
		}
	}))
	defer upstream.Close()
	_, server := runtimeFixture(t)
	status, result := execute(t, server, Envelope{Provider: "openai", Protocol: "responses", APIBase: upstream.URL, APIKey: "A", Body: json.RawMessage(`{"model":"m","input":"x","stream":true,"context_management":[{"type":"compaction","compact_threshold":1000}]}`)})
	if status != 200 || !bytes.Contains(result, []byte("opaque-signed-payload")) || !bytes.Contains(result, []byte("call_1")) || bytes.Contains(result, []byte("[DONE]")) {
		t.Fatalf("native events lost %d %s", status, result)
	}
}
