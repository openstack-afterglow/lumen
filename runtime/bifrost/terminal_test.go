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
	"time"
)

func TestProviderTerminalCompletesWithoutUpstreamEOF(t *testing.T) {
	for _, protocol := range []string{"chat", "responses", "messages"} {
		t.Run(protocol, func(t *testing.T) {
			var calls atomic.Int32
			payload := "data: {\"id\":\"chat\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"accepted\"},\"finish_reason\":null}]}\n\ndata: [DONE]\n\n"
			provider := "openai"
			if protocol == "responses" {
				payload = "data: {\"type\":\"response.completed\",\"response\":{\"status\":\"completed\",\"output\":[],\"usage\":{\"input_tokens\":1,\"output_tokens\":0}}}\n\n"
			} else if protocol == "messages" {
				provider = "anthropic"
				payload = "data: {\"type\":\"message_start\",\"message\":{\"id\":\"msg\",\"type\":\"message\",\"role\":\"assistant\",\"content\":[],\"usage\":{\"input_tokens\":1,\"output_tokens\":0}}}\n\ndata: {\"type\":\"message_stop\"}\n\n"
			}
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				_, _ = io.Copy(io.Discard, r.Body)
				w.Header().Set("Content-Type", "text/event-stream")
				_, _ = io.WriteString(w, payload)
				w.(http.Flusher).Flush()
				// Real SSE peers may keep the socket open after the terminal frame.
				<-r.Context().Done()
			}))
			t.Cleanup(func() { upstream.CloseClientConnections(); upstream.Close() })
			_, server := runtimeFixture(t)
			requestBody := json.RawMessage(`{"model":"fixture","messages":[{"role":"user","content":"hello"}],"max_tokens":16,"stream":true}`)
			if protocol == "responses" {
				requestBody = json.RawMessage(`{"model":"fixture","input":"hello","stream":true}`)
			}
			envelope := Envelope{Protocol: protocol, Provider: provider, APIBase: upstream.URL, APIKey: "synthetic-key", Body: requestBody}
			raw, _ := json.Marshal(envelope)
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			defer cancel()
			req, _ := http.NewRequestWithContext(ctx, http.MethodPost, server.URL+"/v1/execute", bytes.NewReader(raw))
			req.Header.Set("Authorization", "Bearer private-test-token")
			response, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			defer response.Body.Close()
			body, err := io.ReadAll(response.Body)
			if err != nil {
				t.Fatalf("provider terminal still waits for EOF: %v; body=%s", err, body)
			}
			if response.StatusCode != 200 || calls.Load() != 1 {
				t.Fatalf("status=%d attempts=%d", response.StatusCode, calls.Load())
			}
			if protocol == "chat" {
				if strings.Count(string(body), "data: [DONE]") != 1 || !bytes.Contains(body, []byte("accepted")) {
					t.Fatalf("chat terminal/output lost: %s", body)
				}
			} else if bytes.Contains(body, []byte("[DONE]")) || !bytes.Contains(body, []byte(map[string]string{"responses": "response.completed", "messages": "message_stop"}[protocol])) {
				t.Fatalf("native terminal changed: %s", body)
			}
		})
	}
}
