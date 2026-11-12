package bifrost

import (
	"bufio"
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"strings"

	"github.com/maximhq/bifrost/core/providers/anthropic"
	"github.com/maximhq/bifrost/core/providers/cohere"
	"github.com/maximhq/bifrost/core/providers/gemini"
	"github.com/maximhq/bifrost/core/schemas"
)

func responseConverter(ctx *schemas.BifrostContext, wire, protocol, model string) func([]byte) ([]byte, error) {
	return func(raw []byte) ([]byte, error) {
		var chat *schemas.BifrostChatResponse
		var responses *schemas.BifrostResponsesResponse
		switch wire {
		case "chat":
			if protocol == "chat" {
				return raw, nil
			}
			chat = &schemas.BifrostChatResponse{}
			if schemas.Unmarshal(raw, chat) != nil {
				return nil, errUnsupported
			}
			responses = chat.ToBifrostResponsesResponse()
		case "responses":
			responses = &schemas.BifrostResponsesResponse{}
			if schemas.Unmarshal(raw, responses) != nil {
				return nil, errUnsupported
			}
			chat = responses.ToBifrostChatResponse()
		case "anthropic":
			var native anthropic.AnthropicMessageResponse
			if schemas.Unmarshal(raw, &native) != nil {
				return nil, errUnsupported
			}
			chat = native.ToBifrostChatResponse(ctx)
			responses = native.ToBifrostResponsesResponse(ctx)
		case "cohere":
			var native cohere.CohereChatResponse
			if schemas.Unmarshal(raw, &native) != nil {
				return nil, errUnsupported
			}
			chat = native.ToBifrostChatResponse(model)
			responses = chat.ToBifrostResponsesResponse()
		case "gemini":
			var native gemini.GenerateContentResponse
			if schemas.Unmarshal(raw, &native) != nil {
				return nil, errUnsupported
			}
			chat = native.ToBifrostChatResponse()
			responses = native.ToResponsesBifrostResponsesResponse()
		case "gemini_count":
			var native struct {
				TotalTokens *int `json:"totalTokens"`
			}
			if json.Unmarshal(raw, &native) != nil || native.TotalTokens == nil {
				return nil, errUnsupported
			}
			return json.Marshal(map[string]any{"input_tokens": *native.TotalTokens})
		default:
			return nil, errUnsupported
		}
		var result any
		switch protocol {
		case "chat":
			result = chat
		case "responses":
			result = responses
		case "messages":
			result = anthropic.ToAnthropicResponsesResponse(ctx, responses)
		default:
			return nil, errUnsupported
		}
		out, err := schemas.Marshal(result)
		if err != nil {
			return nil, err
		}
		if wire == "anthropic" && protocol == "chat" {
			out = withCompactionResponse(out, raw)
		}
		return preserveMetadata(out, raw, wire), nil
	}
}

// Preserve raw provider accounting/evidence separately from normalized fields. Lumen,
// never Bifrost's model catalog, remains the cost and unknown-vs-zero authority.
func preserveMetadata(converted, raw []byte, wire string) []byte {
	var out map[string]any
	var native map[string]any
	if json.Unmarshal(converted, &out) != nil || json.Unmarshal(raw, &native) != nil {
		return converted
	}
	if usage, ok := native["usage"]; ok {
		out["provider_usage"] = usage
	}
	if usage, ok := native["usageMetadata"]; ok {
		out["provider_usage"] = usage
		out["usage_metadata"] = usage
	}
	if wire == "responses" {
		var reasoning []any
		var annotations []any
		if output, ok := native["output"].([]any); ok {
			for _, v := range output {
				item, ok := v.(map[string]any)
				if !ok {
					continue
				}
				if item["type"] == "reasoning" || item["type"] == "compaction" {
					reasoning = append(reasoning, item)
				}
				if content, ok := item["content"].([]any); ok {
					for _, c := range content {
						if block, ok := c.(map[string]any); ok {
							if a, ok := block["annotations"].([]any); ok {
								annotations = append(annotations, a...)
							}
						}
					}
				}
			}
		}
		attachMessageEvidence(out, reasoning, nil, annotations)
	}
	if wire == "anthropic" {
		var thinking []any
		if content, ok := native["content"].([]any); ok {
			for _, v := range content {
				if item, ok := v.(map[string]any); ok && (item["type"] == "thinking" || item["type"] == "redacted_thinking") {
					thinking = append(thinking, item)
				}
			}
		}
		attachMessageEvidence(out, nil, thinking, nil)
	}
	result, err := json.Marshal(out)
	if err != nil {
		return converted
	}
	return result
}
func attachMessageEvidence(out map[string]any, reasoning, thinking, annotations []any) {
	choices, ok := out["choices"].([]any)
	if !ok || len(choices) == 0 {
		return
	}
	choice, ok := choices[0].(map[string]any)
	if !ok {
		return
	}
	msg, ok := choice["message"].(map[string]any)
	if !ok {
		msg, _ = choice["delta"].(map[string]any)
	}
	if msg == nil {
		return
	}
	if len(reasoning) > 0 {
		msg["reasoning_items"] = reasoning
	}
	if len(thinking) > 0 {
		msg["thinking_blocks"] = thinking
	}
	if len(annotations) > 0 {
		msg["annotations"] = annotations
	}
}

func nativeEvent(protocol string) func([]byte) ([][]byte, bool, error) {
	return func(raw []byte) ([][]byte, bool, error) {
		if bytes.Equal(raw, []byte("[DONE]")) {
			return nil, protocol == "chat", nil
		}
		var e map[string]json.RawMessage
		if json.Unmarshal(raw, &e) != nil {
			return nil, false, errUnsupported
		}
		var kind string
		_ = json.Unmarshal(e["type"], &kind)
		if kind == "error" || kind == "response.failed" || kind == "response.incomplete" || e["error"] != nil {
			return nil, false, errUnsupported
		}
		terminal := kind == "message_stop"
		if protocol == "responses" && kind == "response.completed" {
			var response struct {
				Status string `json:"status"`
			}
			_ = json.Unmarshal(e["response"], &response)
			terminal = response.Status == "completed"
		}
		return [][]byte{raw}, terminal, nil
	}
}

func eventConverter(ctx *schemas.BifrostContext, wire, protocol string, p *executionPlan) func([]byte) ([][]byte, bool, error) {
	var anthropicState *anthropic.AnthropicStreamState
	var geminiState *gemini.GeminiStreamState
	var evidence *anthropicEvidence
	var toolIndexes *responseToolIndexes
	switch wire {
	case "anthropic":
		anthropicState = anthropic.NewAnthropicStreamState()
		evidence = &anthropicEvidence{}
	case "gemini":
		geminiState = gemini.NewGeminiStreamState()
	case "responses":
		toolIndexes = &responseToolIndexes{byOutput: make(map[int]uint16)}
	}
	var chatToResponses *schemas.ChatToResponsesStreamState
	if protocol != "chat" {
		chatToResponses = schemas.AcquireChatToResponsesStreamState()
		previousClose := p.close
		p.close = func() { schemas.ReleaseChatToResponsesStreamState(chatToResponses); previousClose() }
	}
	// Converter maps live for this request only; there is no cross-account session state.
	return func(raw []byte) ([][]byte, bool, error) {
		if bytes.Equal(raw, []byte("[DONE]")) {
			return nil, wire == "chat", nil
		}
		var meta struct {
			Type     string          `json:"type"`
			Error    json.RawMessage `json:"error"`
			Response json.RawMessage `json:"response"`
		}
		if json.Unmarshal(raw, &meta) != nil || meta.Error != nil || meta.Type == "error" || meta.Type == "response.failed" || meta.Type == "response.incomplete" {
			return nil, false, errUnsupported
		}
		var chats []*schemas.BifrostChatResponse
		var terminal bool
		switch wire {
		case "chat":
			if protocol == "chat" {
				return [][]byte{raw}, false, nil
			}
			var chat schemas.BifrostChatResponse
			if schemas.Unmarshal(raw, &chat) != nil {
				return nil, false, errUnsupported
			}
			chats = append(chats, &chat)
		case "responses":
			var event schemas.BifrostResponsesStreamResponse
			if schemas.Unmarshal(raw, &event) != nil {
				return nil, false, errUnsupported
			}
			var nativeResponse struct {
				Status string `json:"status"`
			}
			_ = json.Unmarshal(meta.Response, &nativeResponse)
			terminal = meta.Type == "response.completed" && nativeResponse.Status == "completed"
			chat := event.ToBifrostChatResponse()
			if toolIndexes.apply(&event, chat) != nil {
				return nil, false, errUnsupported
			}
			if chat != nil {
				encoded, _ := schemas.Marshal(chat)
				if len(meta.Response) > 0 {
					encoded = preserveMetadata(encoded, meta.Response, "responses")
				}
				if protocol == "chat" {
					return [][]byte{encoded}, terminal, nil
				}
				chats = append(chats, chat)
			}
		case "anthropic":
			var event anthropic.AnthropicStreamEvent
			if schemas.Unmarshal(raw, &event) != nil {
				return nil, false, errUnsupported
			}
			chat, bErr, _ := event.ToBifrostChatCompletionStream(ctx, "", anthropicState)
			if bErr != nil {
				return nil, false, errUnsupported
			}
			if chat != nil {
				chats = append(chats, chat)
			}
			terminal = meta.Type == "message_stop"
			// Usage and stop reason are merged for every protocol; the pinned chat
			// converter drops both. Thinking/compaction block evidence stays chat-only.
			extra := evidence.observe(raw)
			if terminal {
				if final := evidence.terminalChunk(); final != nil {
					if protocol == "chat" {
						encoded, err := schemas.Marshal(final)
						if err != nil {
							return nil, false, err
						}
						if provider := evidence.providerUsage(); provider != nil && final.Usage != nil {
							encoded = withProviderUsage(encoded, provider)
						}
						var frames [][]byte
						for _, chat := range chats {
							v, err := schemas.Marshal(chat)
							if err != nil {
								return nil, false, err
							}
							frames = append(frames, v)
						}
						return append(frames, encoded), true, nil
					}
					chats = append(chats, final)
				}
			}
			if protocol == "chat" && len(extra) > 0 {
				var encoded [][]byte
				for _, chat := range chats {
					v, err := schemas.Marshal(chat)
					if err != nil {
						return nil, false, err
					}
					encoded = append(encoded, v)
				}
				encoded = append(encoded, extra)
				return encoded, terminal, nil
			}
		case "gemini":
			var event gemini.GenerateContentResponse
			if schemas.Unmarshal(raw, &event) != nil {
				return nil, false, errUnsupported
			}
			var bErr *schemas.BifrostError
			chats, bErr, terminal = event.ToBifrostChatCompletionStream(geminiState)
			if bErr != nil {
				return nil, false, errUnsupported
			}
		case "cohere":
			var event cohere.CohereStreamEvent
			if schemas.Unmarshal(raw, &event) != nil {
				return nil, false, errUnsupported
			}
			converted, bErr, _ := event.ToBifrostChatCompletionStream()
			if bErr != nil {
				return nil, false, errUnsupported
			}
			if converted != nil {
				chats = append(chats, converted)
			}
			terminal = meta.Type == "message-end"
		default:
			return nil, false, errUnsupported
		}
		var output [][]byte
		for _, chat := range chats {
			if protocol == "chat" {
				encoded, err := schemas.Marshal(chat)
				if err != nil {
					return nil, false, err
				}
				output = append(output, preserveMetadata(encoded, raw, wire))
				continue
			}
			events := chat.ToBifrostResponsesStreamResponse(chatToResponses)
			for _, event := range events {
				if protocol == "responses" {
					encoded, err := schemas.Marshal(event)
					if err != nil {
						return nil, false, err
					}
					output = append(output, encoded)
				} else if protocol == "messages" {
					for _, native := range anthropic.ToAnthropicResponsesStreamResponse(ctx, event) {
						encoded, err := schemas.Marshal(native)
						if err != nil {
							return nil, false, err
						}
						output = append(output, encoded)
					}
				}
			}
		}
		return output, terminal, nil
	}
}

// streamResponse parses bounded SSE frames. EOF is never terminal evidence. Invalid,
// truncated, cancelled and provider-error streams close without a manufactured [DONE].
func (p *executionPlan) streamResponse(w http.ResponseWriter, reader io.Reader) {
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.WriteHeader(200)
	flusher, _ := w.(http.Flusher)
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 4096), 8<<20)
	var data []string
	frameBytes := 0
	terminal := false
	var pendingTerminal [][]byte
	writeEvent := func(event []byte) error {
		var meta struct {
			Type string `json:"type"`
		}
		_ = json.Unmarshal(event, &meta)
		if (p.kind == "responses" || p.kind == "messages") && meta.Type != "" {
			if _, err := w.Write([]byte("event: " + meta.Type + "\n")); err != nil {
				return err
			}
		}
		if _, err := w.Write([]byte("data: ")); err != nil {
			return err
		}
		if _, err := w.Write(event); err != nil {
			return err
		}
		_, err := w.Write([]byte("\n\n"))
		return err
	}
	emit := func() error {
		if len(data) == 0 {
			return nil
		}
		payload := []byte(strings.Join(data, "\n"))
		data = nil
		frameBytes = 0
		events, done, err := p.event(payload)
		if err != nil {
			return err
		}
		terminal = terminal || done
		for _, event := range events {
			if len(event) == 0 {
				continue
			}
			var meta struct {
				Type string `json:"type"`
			}
			_ = json.Unmarshal(event, &meta)
			if (meta.Type == "response.completed" || meta.Type == "message_stop") && !terminal {
				pendingTerminal = append(pendingTerminal, event)
				continue
			}
			if err = writeEvent(event); err != nil {
				return err
			}
		}
		if terminal {
			for _, event := range pendingTerminal {
				if err = writeEvent(event); err != nil {
					return err
				}
			}
			pendingTerminal = nil
		}
		if flusher != nil {
			flusher.Flush()
		}
		return nil
	}
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			if emit() != nil {
				return
			}
			if terminal {
				break
			}
			continue
		}
		if strings.HasPrefix(line, "data:") {
			value := strings.TrimPrefix(line, "data:")
			if strings.HasPrefix(value, " ") {
				value = value[1:]
			}
			frameBytes += len(value) + 1
			if frameBytes > 8<<20 {
				return
			}
			data = append(data, value)
		}
	}
	if scanner.Err() != nil || len(data) != 0 || !terminal {
		return
	}
	// Only chat clients use the OpenAI sentinel. Native protocols already carried
	// their provider terminal event, untouched, and must not receive an invented one.
	if p.kind != "messages" && p.kind != "responses" {
		_, _ = w.Write([]byte("data: [DONE]\n\n"))
		if flusher != nil {
			flusher.Flush()
		}
	}
}
