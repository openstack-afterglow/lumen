package bifrost

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"

	"github.com/maximhq/bifrost/core/providers/anthropic"
	"github.com/maximhq/bifrost/core/providers/cohere"
	"github.com/maximhq/bifrost/core/providers/gemini"
	"github.com/maximhq/bifrost/core/providers/openai"
	"github.com/maximhq/bifrost/core/schemas"
)

var errUnsupported = errors.New("unsupported provider operation")

func prepare(parent context.Context, e Envelope) (*executionPlan, error) {
	ctx, cancel := schemas.NewBifrostContextWithCancel(parent)
	success := false
	defer func() {
		if !success {
			cancel()
		}
	}()
	var body map[string]json.RawMessage
	if json.Unmarshal(e.Body, &body) != nil || body == nil {
		return nil, errUnsupported
	}
	var model string
	if json.Unmarshal(body["model"], &model) != nil || model == "" {
		return nil, errUnsupported
	}
	var stream bool
	if v, ok := body["stream"]; ok && json.Unmarshal(v, &stream) != nil {
		return nil, errUnsupported
	}
	headers := make(http.Header)
	// Only caller-owned provider headers. Internal routing/Host/content headers cannot be injected.
	if stream && (e.Protocol == "embeddings" || e.Protocol == "count_tokens") {
		return nil, errUnsupported
	}
	for k, v := range e.Headers {
		key := http.CanonicalHeaderKey(k)
		if strings.ContainsAny(k+v, "\r\n") {
			return nil, errUnsupported
		}
		if strings.HasPrefix(strings.ToLower(k), "x-bf-") || strings.HasPrefix(strings.ToLower(k), "x-lumen-") {
			return nil, errUnsupported
		}
		switch key {
		case "Host", "Content-Length", "Transfer-Encoding", "Connection", "Idempotency-Key", "X-Idempotency-Key", "Proxy-Authorization":
			return nil, errUnsupported
		}
		headers.Set(key, v)
	}
	if headers.Get("Authorization") != "" && e.APIKey != "" {
		return nil, errUnsupported
	}
	p := &executionPlan{body: e.Body, headers: headers, stream: stream, response: identity, kind: e.Protocol, close: cancel}
	base := strings.TrimRight(e.APIBase, "/")
	provider := e.Provider
	if provider == "" {
		provider = "openai"
	}
	switch provider {
	case "openai":
		if base == "" {
			base = "https://api.openai.com/v1"
		}
	case "anthropic":
		if base == "" {
			base = "https://api.anthropic.com/v1"
		}
	case "gemini":
		if base == "" {
			base = "https://generativelanguage.googleapis.com/v1beta"
		}
	case "azure":
		if base == "" {
			return nil, errUnsupported
		}
	case "vertex", "bedrock":
		if base == "" {
			result, err := prepareCloud(ctx, cancel, e, model, stream, headers)
			success = err == nil
			return result, err
		}
	case "ollama":
		if base == "" {
			base = "http://localhost:11434/v1"
		}
	case "openrouter":
		if base == "" {
			base = "https://openrouter.ai/api/v1"
		}
	case "deepseek":
		if base == "" {
			base = "https://api.deepseek.com/v1"
		}
	case "xai":
		if base == "" {
			base = "https://api.x.ai/v1"
		}
	case "perplexity":
		if base == "" {
			base = "https://api.perplexity.ai"
		}
	case "mistral":
		if base == "" {
			base = "https://api.mistral.ai/v1"
		}
	case "groq":
		if base == "" {
			base = "https://api.groq.com/openai/v1"
		}
	case "together_ai", "together":
		if base == "" {
			base = "https://api.together.xyz/v1"
		}
	case "cohere":
		if base == "" {
			base = "https://api.cohere.com/v2"
		}
	default:
		return nil, errUnsupported
	}
	if _, err := validBase(base); err != nil {
		return nil, err
	}
	if e.Protocol == "count_tokens" && provider != "anthropic" && provider != "gemini" && provider != "vertex" {
		return nil, errUnsupported
	}
	if provider == "anthropic" {
		if headers.Get("Authorization") == "" {
			headers.Set("X-Api-Key", e.APIKey)
		} else {
			headers.Del("X-Api-Key")
		}
		if headers.Get("Anthropic-Version") == "" {
			headers.Set("Anthropic-Version", "2023-06-01")
		}
	} else if provider == "gemini" {
		headers.Set("X-Goog-Api-Key", e.APIKey)
	} else if provider == "azure" {
		if headers.Get("Authorization") == "" {
			headers.Set("Api-Key", e.APIKey)
		}
	} else if e.APIKey != "" {
		headers.Set("Authorization", "Bearer "+e.APIKey)
	}

	// Native same-wire operations intentionally bypass typed round-tripping: unknown native
	// fields, signed thinking/compaction items and cache-control TTL must survive unchanged.
	native := (e.Protocol == "responses" && (provider == "openai" || provider == "azure" || (provider == "perplexity" && perplexityAgent(model, e.APIBase)) || provider == "openrouter" || provider == "xai")) ||
		((e.Protocol == "messages" || e.Protocol == "count_tokens") && provider == "anthropic") ||
		(e.Protocol == "embeddings" && provider != "anthropic" && provider != "gemini" && provider != "vertex" && provider != "bedrock" && provider != "cohere")
	if native {
		path := map[string]string{"responses": "responses", "messages": "messages", "count_tokens": "messages/count_tokens", "embeddings": "embeddings"}[e.Protocol]
		p.target = appendPath(versioned(base, provider), path)
		if provider == "anthropic" {
			if applyAnthropicHeaders(ctx, p) != nil {
				return nil, errUnsupported
			}
		}
		p.event = nativeEvent(e.Protocol)
		success = true
		return p, nil
	}
	if e.Protocol != "chat" && e.Protocol != "responses" && e.Protocol != "messages" && e.Protocol != "count_tokens" && e.Protocol != "embeddings" {
		return nil, errUnsupported
	}
	var chat *schemas.BifrostChatRequest
	var responses *schemas.BifrostResponsesRequest
	if e.Protocol == "chat" {
		var req openai.OpenAIChatRequest
		if schemas.Unmarshal(e.Body, &req) != nil {
			return nil, errUnsupported
		}
		chat = req.ToBifrostChatRequest(ctx)
		chat.Provider = schemas.ModelProvider(provider)
		chat.Model = model
		responses = chat.ToResponsesRequest()
	} else if e.Protocol == "responses" {
		var req openai.OpenAIResponsesRequest
		if schemas.Unmarshal(e.Body, &req) != nil {
			return nil, errUnsupported
		}
		responses = req.ToBifrostResponsesRequest(ctx)
		responses.Provider = schemas.ModelProvider(provider)
		responses.Model = model
		chat = responses.ToChatRequest()
	} else if e.Protocol == "messages" || e.Protocol == "count_tokens" {
		var req anthropic.AnthropicMessageRequest
		if schemas.Unmarshal(e.Body, &req) != nil {
			return nil, errUnsupported
		}
		responses = req.ToBifrostResponsesRequest(ctx)
		responses.Provider = schemas.ModelProvider(provider)
		responses.Model = model
		chat = responses.ToChatRequest()
	}
	if e.Protocol == "embeddings" && provider == "bedrock" {
		result, err := prepareCloudAtBase(ctx, cancel, e, base, model, stream, headers)
		success = err == nil
		return result, err
	}
	if e.Protocol == "embeddings" && provider == "cohere" {
		result, err := prepareCohereEmbedding(ctx, cancel, e, base, model, headers)
		success = err == nil
		return result, err
	}
	if e.Protocol == "embeddings" {
		result, err := prepareEmbedding(ctx, cancel, e, base, model, headers)
		success = err == nil
		return result, err
	}
	wire := "chat"
	officialOpenAI := provider == "openai" && (e.APIBase == "" || base == "https://api.openai.com" || base == "https://api.openai.com/v1")
	agent := provider == "perplexity" && perplexityAgent(model, e.APIBase)
	switch {
	case provider == "cohere":
		wire = "cohere"
		request, err := cohere.ToCohereChatCompletionRequest(chat)
		if err != nil {
			return nil, err
		}
		p.body, err = schemas.Marshal(request)
		if err != nil {
			return nil, err
		}
		p.target = appendPath(cohereBase(base), "chat")
	case provider == "anthropic":
		wire = "anthropic"
		var request any
		var err error
		if e.Protocol == "responses" {
			request, err = anthropic.ToAnthropicResponsesRequest(ctx, responses)
		} else {
			request, err = anthropic.ToAnthropicChatRequest(ctx, chat)
		}
		if err != nil {
			return nil, err
		}
		p.body, err = schemas.Marshal(request)
		if err != nil {
			return nil, err
		}
		p.target = appendPath(versioned(base, provider), "messages")
		p.body, err = restoreAnthropicExtensions(p.body, e.Body)
		if err != nil {
			return nil, err
		}
		if applyAnthropicHeaders(ctx, p) != nil {
			return nil, errUnsupported
		}
	case provider == "gemini" || provider == "vertex":
		wire = "gemini"
		request, err := gemini.ToGeminiResponsesRequest(ctx, responses)
		if err != nil {
			return nil, err
		}
		p.body, err = schemas.Marshal(request)
		if err != nil {
			return nil, err
		}
		operation := "generateContent"
		if stream {
			operation = "streamGenerateContent?alt=sse"
		}
		p.target = appendPath(base, "models/"+url.PathEscape(strings.TrimPrefix(model, "models/"))+":"+operation)
		if e.Protocol == "count_tokens" {
			p.target = appendPath(base, "models/"+url.PathEscape(model)+":countTokens")
			p.body, _ = json.Marshal(map[string]json.RawMessage{"generateContentRequest": p.body})
			wire = "gemini_count"
		}
	case provider == "bedrock":
		result, err := prepareCloudAtBase(ctx, cancel, e, base, model, stream, headers)
		success = err == nil
		return result, err
	case officialOpenAI || agent:
		wire = "responses"
		// Lumen owns capability decisions and frozen caller controls. The upstream
		// converter raises small output limits and strips model-gated controls;
		// retain its wire/message conversion, not those policy substitutions.
		params := responses.Params
		request := openai.ToOpenAIResponsesRequest(ctx, responses)
		if request == nil {
			return nil, errUnsupported
		}
		if params != nil {
			convertedReasoning := request.Reasoning
			request.ResponsesParameters = *params
			// Token-budget reasoning still needs cross-wire conversion to an effort.
			if e.Protocol != "chat" || params.Reasoning == nil || params.Reasoning.Effort == nil {
				request.Reasoning = convertedReasoning
			}
		}
		// Chat has no typed fields for these native Responses extensions.
		// Restore the original raw values instead of losing them in conversion.
		if include := body["include"]; len(include) > 0 {
			if json.Unmarshal(include, &request.Include) != nil {
				return nil, errUnsupported
			}
		}
		if compaction, requested := body["context_management"]; requested {
			request.ContextManagement = compaction
		}
		var err error
		p.body, err = schemas.Marshal(request)
		if err != nil {
			return nil, err
		}
		if agent {
			base = strings.TrimSuffix(base, "/v1")
		}
		p.target = appendPath(versioned(base, provider), "responses")
	default:
		p.target = appendPath(versioned(base, provider), "chat/completions")
		if e.Protocol != "chat" {
			var err error
			p.body, err = schemas.Marshal(openai.ToOpenAIChatRequest(ctx, chat))
			if err != nil {
				return nil, err
			}
		}
		if provider == "perplexity" && !strings.Contains(base, "/router") {
			p.target = appendPath(base, "chat/completions")
			var payload map[string]json.RawMessage
			_ = json.Unmarshal(p.body, &payload)
			payload["model"], _ = json.Marshal(strings.TrimPrefix(model, "perplexity/"))
			p.body, _ = json.Marshal(payload)
		}
	}
	p.kind = e.Protocol
	p.body = setStream(p.body, stream, wire)
	p.response = responseConverter(ctx, wire, e.Protocol, model)
	if stream {
		p.event = eventConverter(ctx, wire, e.Protocol, p)
	}
	success = true
	return p, nil
}

func identity(raw []byte) ([]byte, error) { return raw, nil }
func appendPath(base, path string) string { return strings.TrimRight(base, "/") + "/" + path }
func versioned(base, provider string) string {
	u, _ := url.Parse(base)
	if provider == "azure" && u != nil && (u.Path == "" || u.Path == "/") {
		return strings.TrimRight(base, "/") + "/openai/v1"
	}
	if u != nil && (u.Path == "" || u.Path == "/") && provider != "perplexity" {
		return strings.TrimRight(base, "/") + "/v1"
	}
	if provider == "perplexity" {
		if strings.HasSuffix(base, "/router") {
			return base + "/v1"
		}
		if !strings.HasSuffix(base, "/v1") && !strings.HasSuffix(base, "/router/v1") {
			return base + "/v1"
		}
	}
	return base
}
func perplexityAgent(model, base string) bool {
	u, _ := url.Parse(base)
	path := ""
	if u != nil {
		path = strings.TrimRight(u.Path, "/")
	}
	if strings.HasSuffix(path, "/router") || strings.HasSuffix(path, "/router/v1") {
		return false
	}
	if strings.HasSuffix(path, "/v1") {
		return true
	}
	return strings.Contains(strings.TrimPrefix(model, "perplexity/"), "/") || (strings.Contains(model, "/") && !strings.HasPrefix(model, "perplexity/"))
}
func setStream(raw []byte, stream bool, wire string) []byte {
	if wire == "gemini" || wire == "gemini_count" {
		return raw
	}
	var body map[string]json.RawMessage
	_ = json.Unmarshal(raw, &body)
	body["stream"] = json.RawMessage(fmt.Sprint(stream))
	out, _ := json.Marshal(body)
	return out
}
