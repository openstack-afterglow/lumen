// Package bifrost implements Lumen's private, request-local provider execution boundary.
package bifrost

import (
	"bytes"
	"context"
	"crypto/subtle"
	"crypto/tls"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync/atomic"
	"time"
)

const Version = "v1.13.2-0.20261021235121-668a660e5ac0"
const Contract = "single-attempt-v1"
const ContractHeader = "X-Lumen-Bifrost-Contract"

var Protocols = []string{"chat", "responses", "messages", "count_tokens", "embeddings"}

// Envelope is never persisted or logged. Authentication material belongs to this invocation.
type Envelope struct {
	Protocol      string            `json:"protocol"`
	Provider      string            `json:"provider"`
	APIBase       string            `json:"api_base"`
	APIKey        string            `json:"api_key"`
	Headers       map[string]string `json:"headers"`
	Body          json.RawMessage   `json:"body"`
	SingleAttempt *bool             `json:"single_attempt"`
}

type Server struct {
	token    string
	client   *http.Client
	draining atomic.Bool
}

func NewServer(token string) (*Server, error) {
	if strings.TrimSpace(token) == "" {
		return nil, errors.New("internal authentication token is required")
	}
	// A fresh HTTP/1 connection per invocation removes stale-connection replay. No proxy,
	// redirect, GetBody, idempotency header, HTTP/2 retry, or provider SDK retry participates.
	transport := &http.Transport{
		Proxy: nil, DialContext: (&net.Dialer{Timeout: 30 * time.Second, KeepAlive: -1}).DialContext,
		DisableKeepAlives: true, ForceAttemptHTTP2: false,
		TLSHandshakeTimeout: 30 * time.Second, ResponseHeaderTimeout: 10 * time.Minute,
		TLSNextProto:    map[string]func(string, *tls.Conn) http.RoundTripper{},
		TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS12, NextProtos: []string{"http/1.1"}},
	}
	return &Server{token: token, client: &http.Client{Transport: transport,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}}, nil
}

func (s *Server) Drain() { s.draining.Store(true); s.client.CloseIdleConnections() }

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set(ContractHeader, Contract)
	if r.URL.Path == "/health" && r.Method == http.MethodGet {
		writeJSON(w, 200, map[string]bool{"ok": true})
		return
	}
	supplied := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	if supplied == r.Header.Get("Authorization") || subtle.ConstantTimeCompare([]byte(supplied), []byte(s.token)) != 1 {
		fail(w, 401, "runtime_authentication_failed")
		return
	}
	if s.draining.Load() {
		fail(w, 503, "runtime_draining")
		return
	}
	if r.URL.Path == "/ready" && r.Method == http.MethodGet {
		writeJSON(w, 200, map[string]any{"ready": true, "contract": Contract, "version": Version, "supported_protocols": Protocols})
		return
	}
	if r.URL.Path != "/v1/execute" || r.Method != http.MethodPost {
		fail(w, 404, "not_found")
		return
	}
	var e Envelope
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<20))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&e) != nil || decoder.Decode(&struct{}{}) != io.EOF {
		fail(w, 400, "invalid_envelope")
		return
	}
	if e.SingleAttempt != nil && !*e.SingleAttempt {
		fail(w, 400, "single_attempt_required")
		return
	}
	if len(e.Body) == 0 || !json.Valid(e.Body) {
		fail(w, 400, "invalid_body")
		return
	}
	plan, err := prepare(r.Context(), e)
	if err != nil {
		fail(w, 400, "unsupported_provider_request")
		return
	}
	defer plan.close()
	req, err := http.NewRequestWithContext(r.Context(), http.MethodPost, plan.target, bytes.NewReader(plan.body))
	if err != nil {
		fail(w, 400, "invalid_upstream_target")
		return
	}
	req.GetBody = nil
	req.Close = true
	req.Header = plan.headers
	req.Header.Set("Content-Type", "application/json")
	if plan.sign != nil {
		if plan.sign(req) != nil {
			fail(w, 400, "invalid_upstream_credentials")
			return
		}
	}
	// Deliberately call RoundTrip, not Client.Do: redirects cannot change target or auth.
	resp, err := s.client.Transport.RoundTrip(req)
	if err != nil {
		if r.Context().Err() != nil {
			return
		}
		status := 502
		var nerr net.Error
		if errors.As(err, &nerr) && nerr.Timeout() {
			status = 504
		}
		fail(w, status, "upstream_connection_failed")
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		status := resp.StatusCode
		if status < 400 || status > 599 {
			status = 502
		}
		fail(w, status, "upstream_rejected")
		return
	}
	if plan.stream {
		var reader io.Reader = resp.Body
		if plan.wrapStream != nil {
			framed := plan.wrapStream(resp.Body)
			defer framed.Close()
			reader = framed
		}
		plan.streamResponse(w, reader)
		return
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 64<<20+1))
	if err != nil || len(raw) > 64<<20 || !json.Valid(raw) {
		fail(w, 502, "invalid_upstream_response")
		return
	}
	result, err := plan.response(raw)
	if err != nil {
		fail(w, 502, "invalid_upstream_response")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(resp.StatusCode)
	_, _ = w.Write(result)
}

func fail(w http.ResponseWriter, status int, code string) {
	writeJSON(w, status, map[string]any{"error": map[string]string{"type": code, "code": code, "message": "Provider request failed"}})
}
func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}
func validBase(base string) (*url.URL, error) {
	u, err := url.Parse(base)
	if err != nil || u.Host == "" || (u.Scheme != "https" && u.Scheme != "http") || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return nil, errors.New("invalid upstream base")
	}
	return u, nil
}

// A plan owns converter state and cancellation for exactly one execution.
type executionPlan struct {
	target     string
	body       []byte
	headers    http.Header
	stream     bool
	kind       string
	response   func([]byte) ([]byte, error)
	event      func([]byte) ([][]byte, bool, error)
	close      context.CancelFunc
	sign       func(*http.Request) error
	wrapStream func(io.Reader) io.ReadCloser
}
