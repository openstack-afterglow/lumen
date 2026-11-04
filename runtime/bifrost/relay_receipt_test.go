package bifrost

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
)

func TestDispatchReceiptUsesActualConvertedDestinationAndStatus(t *testing.T) {
	for _, status := range []int{200, 401, 500} {
		var sentPath, sentKey string
		upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			sentPath, sentKey = r.URL.Path, r.Header.Get("Authorization")
			w.Header().Set("X-Lumen-Upstream-Endpoint", "https://spoofed.example/secret")
			w.WriteHeader(status)
			_, _ = w.Write([]byte(`{"id":"safe","choices":[{"message":{"role":"assistant","content":"hello"},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1}}`))
		}))
		_, runtime := runtimeFixture(t)
		envelope := chatEnvelope(upstream.URL)
		encoded, _ := json.Marshal(envelope)
		request, _ := http.NewRequest("POST", runtime.URL+"/v1/execute", bytes.NewReader(encoded))
		request.Header.Set("Authorization", "Bearer private-test-token")
		response, err := http.DefaultClient.Do(request)
		if err != nil {
			t.Fatal(err)
		}
		_, _ = io.Copy(io.Discard, response.Body)
		_ = response.Body.Close()
		if sentPath != "/v1/chat/completions" || sentKey != "Bearer key-A" {
			t.Fatalf("unexpected dispatch path=%s key=%s", sentPath, sentKey)
		}
		if response.Header.Get("X-Lumen-Upstream-Endpoint") != upstream.URL+sentPath {
			t.Fatal("receipt did not come from actual converted target")
		}
		if response.Header.Get("X-Lumen-Upstream-Status") != strconv.Itoa(status) {
			t.Fatal("receipt lost exact upstream status")
		}
		upstream.Close()
	}
}


func TestInvalidSecretBearingBaseHasNoDispatchReceipt(t *testing.T) {
	_, runtime := runtimeFixture(t)
	for _, base := range []string{"https://user:secret@example.invalid/v1", "https://example.invalid/v1?key=secret", "https://example.invalid/v1#secret"} {
		encoded, _ := json.Marshal(chatEnvelope(base))
		request, _ := http.NewRequest("POST", runtime.URL+"/v1/execute", bytes.NewReader(encoded))
		request.Header.Set("Authorization", "Bearer private-test-token")
		response, err := http.DefaultClient.Do(request)
		if err != nil {
			t.Fatal(err)
		}
		_, _ = io.Copy(io.Discard, response.Body)
		_ = response.Body.Close()
		if response.StatusCode != 400 || response.Header.Get("X-Lumen-Upstream-Endpoint") != "" || response.Header.Get("X-Lumen-Upstream-Status") != "" {
			t.Fatal("invalid credential-bearing base must not dispatch or emit a receipt")
		}
	}
}
