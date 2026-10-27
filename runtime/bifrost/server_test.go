package bifrost

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func runtimeFixture(t *testing.T) (*Server, *httptest.Server) {
	t.Helper()
	handler, err := NewServer("private-test-token")
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	return handler, server
}
func execute(t *testing.T, server *httptest.Server, e Envelope) (int, []byte) {
	t.Helper()
	encoded, err := json.Marshal(e)
	if err != nil {
		t.Fatal(err)
	}
	req, err := http.NewRequest("POST", server.URL+"/v1/execute", bytes.NewReader(encoded))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer private-test-token")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.Header.Get(ContractHeader) != Contract {
		t.Fatal("missing single-attempt proof")
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	return resp.StatusCode, body
}
func chatEnvelope(base string) Envelope {
	return Envelope{Protocol: "chat", Provider: "openai", APIBase: base, APIKey: "key-A", Body: json.RawMessage(`{"model":"synthetic-model","messages":[{"role":"user","content":"hello"}],"max_tokens":24}`)}
}

func TestAuthenticationReadinessAndDrain(t *testing.T) {
	handler, server := runtimeFixture(t)
	resp, err := http.Get(server.URL + "/ready")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 401 {
		t.Fatal(resp.StatusCode)
	}
	req, _ := http.NewRequest("GET", server.URL+"/ready", nil)
	req.Header.Set("Authorization", "Bearer private-test-token")
	resp, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var ready map[string]any
	if json.NewDecoder(resp.Body).Decode(&ready) != nil || ready["version"] != Version || ready["contract"] != Contract {
		t.Fatal(ready)
	}
	handler.Drain()
	status, _ := execute(t, server, chatEnvelope("http://127.0.0.1:1"))
	if status != 503 {
		t.Fatal(status)
	}
}
func TestExactlyOneUpstreamAttemptForEveryFailure(t *testing.T) {
	for _, status := range []int{400, 401, 403, 429, 500} {
		t.Run(fmt.Sprint(status), func(t *testing.T) {
			var calls atomic.Int32
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				w.WriteHeader(status)
				_, _ = w.Write([]byte(`{"error":{"message":"key-A secret encrypted reasoning repair request"}}`))
			}))
			defer upstream.Close()
			_, server := runtimeFixture(t)
			got, body := execute(t, server, chatEnvelope(upstream.URL))
			if got != status || calls.Load() != 1 {
				t.Fatalf("status=%d calls=%d", got, calls.Load())
			}
			if bytes.Contains(body, []byte("key-A")) || bytes.Contains(body, []byte("encrypted")) {
				t.Fatal("upstream error leaked")
			}
		})
	}
	t.Run("network-after-send", func(t *testing.T) {
		var calls atomic.Int32
		upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			calls.Add(1)
			conn, _, _ := w.(http.Hijacker).Hijack()
			_ = conn.Close()
		}))
		defer upstream.Close()
		_, server := runtimeFixture(t)
		status, _ := execute(t, server, chatEnvelope(upstream.URL))
		if status != 502 || calls.Load() != 1 {
			t.Fatalf("%d calls %d", status, calls.Load())
		}
	})
	t.Run("redirect", func(t *testing.T) {
		var calls atomic.Int32
		upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1); http.Redirect(w, r, "/retry", 307) }))
		defer upstream.Close()
		_, server := runtimeFixture(t)
		status, _ := execute(t, server, chatEnvelope(upstream.URL))
		if status != 502 || calls.Load() != 1 {
			t.Fatalf("%d calls %d", status, calls.Load())
		}
	})
}
func TestFalseSingleAttemptAndInjectedIdempotencyRefusedBeforeIO(t *testing.T) {
	var calls atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1) }))
	defer upstream.Close()
	_, server := runtimeFixture(t)
	e := chatEnvelope(upstream.URL)
	value := false
	e.SingleAttempt = &value
	status, _ := execute(t, server, e)
	if status != 400 {
		t.Fatal(status)
	}
	e.SingleAttempt = nil
	e.Headers = map[string]string{"Idempotency-Key": "repair"}
	status, _ = execute(t, server, e)
	if status != 400 || calls.Load() != 0 {
		t.Fatalf("%d calls %d", status, calls.Load())
	}
}
func TestCredentialsNeverMixAcrossConcurrentRequests(t *testing.T) {
	var calls atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		if r.Header.Get("Authorization") != "Bearer "+body["model"].(string) {
			t.Error("credential and model mixed")
		}
		_, _ = w.Write([]byte(`{"id":"x","object":"chat.completion","choices":[],"usage":{"prompt_tokens":1,"completion_tokens":0}}`))
	}))
	defer upstream.Close()
	_, server := runtimeFixture(t)
	var wg sync.WaitGroup
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			key := fmt.Sprintf("selected-key-%d", i)
			e := chatEnvelope(upstream.URL)
			e.APIKey = key
			e.Body = json.RawMessage(fmt.Sprintf(`{"model":%q,"messages":[{"role":"user","content":"x"}]}`, key))
			status, _ := execute(t, server, e)
			if status != 200 {
				t.Error(status)
			}
		}(i)
	}
	wg.Wait()
	if calls.Load() != 12 {
		t.Fatal(calls.Load())
	}
}
func TestStreamTruncationDoesNotManufactureTerminal(t *testing.T) {
	for _, terminal := range []bool{false, true} {
		t.Run(fmt.Sprint(terminal), func(t *testing.T) {
			var calls atomic.Int32
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				w.Header().Set("Content-Type", "text/event-stream")
				_, _ = w.Write([]byte(`data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"lookup","arguments":"{}"}}]}}]}` + "\n\n"))
				if terminal {
					_, _ = w.Write([]byte("data: [DONE]\n\n"))
				}
			}))
			defer upstream.Close()
			_, server := runtimeFixture(t)
			e := chatEnvelope(upstream.URL)
			e.Body = json.RawMessage(`{"model":"m","messages":[],"stream":true}`)
			status, body := execute(t, server, e)
			if status != 200 || calls.Load() != 1 || bytes.Contains(body, []byte("[DONE]")) != terminal {
				t.Fatalf("%d %d %s", status, calls.Load(), body)
			}
			if !bytes.Contains(body, []byte("call_1")) {
				t.Fatal("tool delta lost")
			}
		})
	}
}
func TestCancellationClosesActualUpstream(t *testing.T) {
	started := make(chan struct{})
	cancelled := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, err := io.Copy(io.Discard, r.Body); err != nil {
			t.Error(err)
			return
		}
		close(started)
		<-r.Context().Done()
		close(cancelled)
	}))
	defer upstream.Close()
	defer upstream.CloseClientConnections()
	_, server := runtimeFixture(t)
	e := chatEnvelope(upstream.URL)
	raw, _ := json.Marshal(e)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, "POST", server.URL+"/v1/execute", bytes.NewReader(raw))
	req.Header.Set("Authorization", "Bearer private-test-token")
	finished := make(chan struct{})
	go func() {
		resp, _ := http.DefaultClient.Do(req)
		if resp != nil {
			_ = resp.Body.Close()
		}
		close(finished)
	}()
	select {
	case <-started:
	case <-time.After(3 * time.Second):
		t.Fatal("upstream did not start")
	}
	cancel()
	select {
	case <-cancelled:
	case <-time.After(3 * time.Second):
		t.Fatal("upstream did not cancel")
	}
	<-finished
}
func TestNativeMessagesCountAndCompactionHaveNoRoundTripLoss(t *testing.T) {
	for _, protocol := range []string{"messages", "count_tokens"} {
		t.Run(protocol, func(t *testing.T) {
			var observed []byte
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				observed, _ = io.ReadAll(r.Body)
				if r.Header.Get("Authorization") != "Bearer oauth-token" || r.Header.Get("X-Api-Key") != "" {
					t.Error("OAuth key mixed")
				}
				if protocol == "count_tokens" {
					_, _ = w.Write([]byte(`{"input_tokens":19}`))
				} else {
					_, _ = w.Write([]byte(`{"id":"m","type":"message","future_native":true,"content":[{"type":"compaction","content":"summary"}],"usage":{"input_tokens":19,"output_tokens":3}}`))
				}
			}))
			defer upstream.Close()
			_, server := runtimeFixture(t)
			body := json.RawMessage(`{"model":"claude-sonnet-4-6","max_tokens":51,"future_field":{"keep":true},"messages":[{"role":"assistant","content":[{"type":"compaction","content":"summary"},{"type":"tool_use","id":"call_1","name":"lookup","input":{}}]},{"role":"user","content":[{"type":"tool_result","tool_use_id":"call_1","content":"answer"}]}]}`)
			status, result := execute(t, server, Envelope{Provider: "anthropic", Protocol: protocol, APIBase: upstream.URL, Headers: map[string]string{"Authorization": "Bearer oauth-token"}, Body: body})
			if status != 200 || !bytes.Equal(observed, body) {
				t.Fatalf("lossy native payload: %d %s", status, observed)
			}
			if protocol == "messages" && !bytes.Contains(result, []byte("future_native")) {
				t.Fatal("native response field lost")
			}
		})
	}
}

type rewriteTransport struct {
	target *url.URL
	inner  http.RoundTripper
}

func (transport rewriteTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	clone := req.Clone(req.Context())
	u := *req.URL
	u.Scheme = transport.target.Scheme
	u.Host = transport.target.Host
	clone.URL = &u
	clone.Host = u.Host
	return transport.inner.RoundTrip(clone)
}
func TestOfficialOpenAIUsesBifrostResponsesBridge(t *testing.T) {
	var path string
	var request map[string]any
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path = r.URL.Path
		_ = json.NewDecoder(r.Body).Decode(&request)
		_, _ = w.Write([]byte(`{"id":"resp_1","object":"response","model":"gpt-4.1-mini","status":"completed","output":[{"id":"msg_1","type":"message","role":"assistant","content":[{"type":"output_text","text":"hello","annotations":[]}]}],"usage":{"input_tokens":2,"output_tokens":3,"total_tokens":5}}`))
	}))
	defer upstream.Close()
	handler, server := runtimeFixture(t)
	target, _ := url.Parse(upstream.URL)
	handler.client.Transport = rewriteTransport{target: target, inner: handler.client.Transport}
	e := chatEnvelope("")
	e.Body = json.RawMessage(`{"model":"gpt-4.1-mini","messages":[{"role":"user","content":"hello"}],"max_tokens":13,"reasoning_effort":"none","temperature":0,"top_p":0,"include":["reasoning.encrypted_content"],"context_management":[{"type":"compaction","compact_threshold":1000}]}`)
	status, result := execute(t, server, e)
	if status != 200 || path != "/v1/responses" || request["max_output_tokens"] != float64(13) || request["input"] == nil {
		t.Fatalf("status=%d path=%s request=%v", status, path, request)
	}
	reasoning, ok := request["reasoning"].(map[string]any)
	if !ok || reasoning["effort"] != "none" || request["temperature"] != float64(0) || request["top_p"] != float64(0) {
		t.Fatalf("caller controls changed: %v", request)
	}
	include, _ := json.Marshal(request["include"])
	compaction, _ := json.Marshal(request["context_management"])
	if string(include) != `["reasoning.encrypted_content"]` || string(compaction) != `[{"compact_threshold":1000,"type":"compaction"}]` {
		t.Fatalf("opaque controls lost: %v", request)
	}
	if !bytes.Contains(result, []byte("hello")) || !bytes.Contains(result, []byte("prompt_tokens")) {
		t.Fatalf("bridge lost output %s", result)
	}
}
func TestAnthropicChatConversionPreservesPendingToolAndCompactionHead(t *testing.T) {
	plan, err := prepare(context.Background(), Envelope{Provider: "anthropic", Protocol: "chat", APIKey: "a", Body: json.RawMessage(`{"model":"claude-sonnet-4-6","max_tokens":77,"context_management":{"edits":[{"type":"compact_20260112","trigger":{"type":"input_tokens","value":100000}}]},"messages":[{"role":"user","content":"question"},{"role":"assistant","content":null,"tool_calls":[{"id":"call_1","type":"function","function":{"name":"lookup","arguments":"{}"}}],"provider_specific_fields":{"compaction_blocks":[{"type":"compaction","content":"summary"}]}},{"role":"tool","tool_call_id":"call_1","content":"answer"}]}`)})
	if err != nil {
		t.Fatal(err)
	}
	defer plan.close()
	var body struct {
		Messages []struct {
			Content json.RawMessage `json:"content"`
		} `json:"messages"`
		MaxTokens         int `json:"max_tokens"`
		ContextManagement any `json:"context_management"`
	}
	if json.Unmarshal(plan.body, &body) != nil || len(body.Messages) != 3 || body.MaxTokens != 77 || body.ContextManagement == nil {
		t.Fatalf("bad conversion %s", plan.body)
	}
	var assistant, result []map[string]any
	if json.Unmarshal(body.Messages[1].Content, &assistant) != nil || json.Unmarshal(body.Messages[2].Content, &result) != nil || len(assistant) < 2 || len(result) < 1 {
		t.Fatalf("missing tool exchange %s", plan.body)
	}
	if assistant[0]["type"] != "compaction" || assistant[1]["id"] != "call_1" || result[0]["tool_use_id"] != "call_1" {
		t.Fatalf("pending tool broken %s", plan.body)
	}
}
func TestAnthropicCompactionUsageAggregatesAllIterationsOnce(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"id":"m","type":"message","role":"assistant","model":"claude-sonnet-4-6","content":[{"type":"text","text":"ok"}],"stop_reason":"end_turn","usage":{"input_tokens":10,"output_tokens":5,"cache_read_input_tokens":100,"cache_creation_input_tokens":30,"cache_creation":{"ephemeral_5m_input_tokens":20,"ephemeral_1h_input_tokens":10},"iterations":[{"type":"compaction","input_tokens":200,"output_tokens":40,"cache_read_input_tokens":50,"cache_creation_input_tokens":0},{"type":"message","input_tokens":10,"output_tokens":5,"cache_read_input_tokens":100,"cache_creation_input_tokens":30,"cache_creation":{"ephemeral_5m_input_tokens":20,"ephemeral_1h_input_tokens":10}}]}}`))
	}))
	defer upstream.Close()
	_, server := runtimeFixture(t)
	e := chatEnvelope(upstream.URL)
	e.Provider = "anthropic"
	e.Body = json.RawMessage(`{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"hi"}],"max_tokens":64}`)
	status, result := execute(t, server, e)
	var body struct {
		Usage struct {
			PromptTokens     int `json:"prompt_tokens"`
			CompletionTokens int `json:"completion_tokens"`
		} `json:"usage"`
	}
	_ = json.Unmarshal(result, &body)
	if status != 200 || body.Usage.PromptTokens != 390 || body.Usage.CompletionTokens != 45 {
		t.Fatalf("double-count or lost compaction: %d %s", status, result)
	}
}
func TestProviderRoutesAndNoCredentialLoggingSurface(t *testing.T) {
	for _, provider := range []string{"openai", "anthropic", "gemini", "azure", "vertex", "ollama", "openrouter", "deepseek", "xai", "perplexity"} {
		body := json.RawMessage(`{"model":"model","messages":[{"role":"user","content":"hello"}],"max_tokens":10}`)
		plan, err := prepare(context.Background(), Envelope{Provider: provider, Protocol: "chat", APIBase: "http://127.0.0.1:43210/v1", APIKey: "secret-value", Body: body})
		if err != nil {
			t.Fatalf("%s: %v", provider, err)
		}
		plan.close()
		if strings.Contains(plan.target, "secret-value") {
			t.Fatal("credential placed in URL")
		}
	}
}
