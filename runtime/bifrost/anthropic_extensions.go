package bifrost

import (
	"encoding/json"
	"github.com/maximhq/bifrost/core/providers/anthropic"
	"github.com/maximhq/bifrost/core/schemas"
	"strings"
)

// restoreAnthropicExtensions covers native fields outside the neutral chat schema.
// It never changes or retries a rejected payload and never invents a budget.
func restoreAnthropicExtensions(converted, original []byte) ([]byte, error) {
	var out, source map[string]json.RawMessage
	if json.Unmarshal(converted, &out) != nil || json.Unmarshal(original, &source) != nil {
		return nil, errUnsupported
	}
	for _, key := range []string{"thinking", "context_management", "output_config", "metadata", "service_tier", "container"} {
		if value, ok := source[key]; ok {
			out[key] = value
		}
	}
	var input, output []map[string]json.RawMessage
	if json.Unmarshal(source["messages"], &input) != nil || json.Unmarshal(out["messages"], &output) != nil {
		return json.Marshal(out)
	}
	destination := 0
	for _, message := range input {
		var role string
		_ = json.Unmarshal(message["role"], &role)
		// Bifrost groups consecutive tool results into one user message. Align only
		// assistant messages, whose order is stable across that grouping.
		if role != "assistant" {
			continue
		}
		for destination < len(output) {
			var renderedRole string
			_ = json.Unmarshal(output[destination]["role"], &renderedRole)
			if renderedRole == "assistant" {
				break
			}
			destination++
		}
		if destination >= len(output) {
			return nil, errUnsupported
		}
		if role == "assistant" {
			var fields struct {
				CompactionBlocks []json.RawMessage `json:"compaction_blocks"`
				ThinkingBlocks   []json.RawMessage `json:"thinking_blocks"`
			}
			_ = json.Unmarshal(message["provider_specific_fields"], &fields)
			var directThinking []json.RawMessage
			_ = json.Unmarshal(message["thinking_blocks"], &directThinking)
			head := append(fields.CompactionBlocks, fields.ThinkingBlocks...)
			head = append(head, directThinking...)
			if len(head) > 0 {
				var content []json.RawMessage
				if json.Unmarshal(output[destination]["content"], &content) != nil {
					return nil, errUnsupported
				}
				content = append(head, content...)
				encoded, err := json.Marshal(content)
				if err != nil {
					return nil, err
				}
				output[destination]["content"] = encoded
			}
		}
		destination++
	}
	encoded, err := json.Marshal(output)
	if err != nil {
		return nil, err
	}
	out["messages"] = encoded
	return json.Marshal(out)
}

type anthropicEvidence struct {
	blocks map[int]map[string]any
	// Anthropic reports input and cache counters on message_start and output on
	// message_delta. The pinned chat converter drops both, so keep the raw
	// cumulative usage here and surface it as one terminal chunk. A counter the
	// provider never sent stays absent: Lumen bills unknown differently from zero.
	usage     map[string]any
	stopped   bool
	stopSeen  string
	terminate bool
}

func (state *anthropicEvidence) observe(raw []byte) []byte {
	var event struct {
		Type         string         `json:"type"`
		Index        int            `json:"index"`
		ContentBlock map[string]any `json:"content_block"`
		Delta        map[string]any `json:"delta"`
		Usage        map[string]any `json:"usage"`
		Message      struct {
			Usage map[string]any `json:"usage"`
		} `json:"message"`
	}
	if json.Unmarshal(raw, &event) != nil {
		return nil
	}
	switch event.Type {
	case "message_start":
		state.mergeUsage(event.Message.Usage)
		return nil
	case "message_delta":
		state.mergeUsage(event.Usage)
		if reason, _ := event.Delta["stop_reason"].(string); reason != "" {
			state.stopSeen = reason
		}
		state.stopped = true
		return nil
	}
	if state.blocks == nil {
		state.blocks = make(map[int]map[string]any)
	}
	if event.Type == "content_block_start" {
		state.blocks[event.Index] = event.ContentBlock
		return nil
	}
	block := state.blocks[event.Index]
	if block == nil {
		return nil
	}
	if event.Type == "content_block_delta" {
		for _, key := range []string{"content", "thinking", "signature"} {
			if text, ok := event.Delta[key].(string); ok {
				previous, _ := block[key].(string)
				block[key] = previous + text
			}
		}
		return nil
	}
	if event.Type != "content_block_stop" {
		return nil
	}
	delete(state.blocks, event.Index)
	kind, _ := block["type"].(string)
	fields := map[string]any{}
	switch kind {
	case "compaction":
		fields["compaction_blocks"] = []any{block}
	case "thinking", "redacted_thinking":
		fields["thinking_blocks"] = []any{block}
	default:
		return nil
	}
	encoded, _ := json.Marshal(map[string]any{"object": "chat.completion.chunk", "choices": []any{map[string]any{"index": 0, "delta": map[string]any{"provider_specific_fields": fields}}}})
	return encoded
}

// mergeUsage applies Anthropic's cumulative semantics: a later non-null report
// replaces an earlier value for the same counter and never erases counters it
// omits. Reports are never added together.
func (state *anthropicEvidence) mergeUsage(report map[string]any) {
	if len(report) == 0 {
		return
	}
	if state.usage == nil {
		state.usage = make(map[string]any, len(report))
	}
	for key, value := range report {
		if value != nil {
			state.usage[key] = value
		}
	}
}

func usageField(usage map[string]any, key string) (int, bool) {
	value, ok := usage[key].(float64)
	if !ok || value < 0 {
		return 0, false
	}
	return int(value), true
}

// billable mirrors the pinned non-stream billableAnthropicUsage: the top-level
// report covers the final message and every compaction iteration is additive.
func (state *anthropicEvidence) billable() map[string]any {
	iterations, _ := state.usage["iterations"].([]any)
	if len(iterations) == 0 {
		return state.usage
	}
	total := make(map[string]any, len(state.usage))
	for key, value := range state.usage {
		total[key] = value
	}
	for _, item := range iterations {
		iteration, _ := item.(map[string]any)
		if iteration == nil || iteration["type"] != "compaction" {
			continue
		}
		for _, key := range []string{"input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"} {
			if add, ok := usageField(iteration, key); ok {
				current, _ := usageField(total, key)
				total[key] = float64(current + add)
			}
		}
	}
	return total
}

// terminalChunk builds the finish reason and provider-reported usage that the
// pinned chat converter drops. It is typed so the chat protocol marshals it and
// the Responses/Messages protocols feed it through ChatToResponsesStreamState,
// which turns finish_reason plus usage into the terminal event. Nothing is
// emitted before message_delta, so a truncated stream never looks complete.
func (state *anthropicEvidence) terminalChunk() *schemas.BifrostChatResponse {
	if !state.stopped || state.terminate {
		return nil
	}
	state.terminate = true
	choice := schemas.BifrostResponseChoice{Index: 0, ChatStreamResponseChoice: &schemas.ChatStreamResponseChoice{Delta: &schemas.ChatStreamResponseChoiceDelta{}}}
	if state.stopSeen != "" {
		reason := anthropic.ConvertAnthropicFinishReasonToBifrost(anthropic.AnthropicStopReason(state.stopSeen))
		choice.FinishReason = &reason
	}
	chunk := &schemas.BifrostChatResponse{Object: "chat.completion.chunk", Choices: []schemas.BifrostResponseChoice{choice}}
	billable := state.billable()
	input, haveInput := usageField(billable, "input_tokens")
	output, haveOutput := usageField(billable, "output_tokens")
	if haveInput || haveOutput {
		read, haveRead := usageField(billable, "cache_read_input_tokens")
		write, haveWrite := usageField(billable, "cache_creation_input_tokens")
		usage := &schemas.BifrostLLMUsage{PromptTokens: input + read + write, CompletionTokens: output}
		usage.TotalTokens = usage.PromptTokens + usage.CompletionTokens
		if haveRead || haveWrite {
			details := &schemas.ChatPromptTokensDetails{CachedReadTokens: read, CachedWriteTokens: write}
			if cache, _ := billable["cache_creation"].(map[string]any); cache != nil {
				five, _ := usageField(cache, "ephemeral_5m_input_tokens")
				hour, _ := usageField(cache, "ephemeral_1h_input_tokens")
				if five > 0 || hour > 0 {
					details.CachedWriteTokenDetails = &schemas.ChatCachedWriteTokenDetails{CachedWriteTokens5m: five, CachedWriteTokens1h: hour}
				}
			}
			usage.PromptTokensDetails = details
		}
		chunk.Usage = usage
	}
	return chunk
}

// providerUsage is the raw cumulative report, preserved for Lumen's own
// cache/modality split; it is never an invented value.
func (state *anthropicEvidence) providerUsage() map[string]any { return state.usage }

// withProviderUsage attaches the raw cumulative report beside the normalized
// usage so Lumen can split cache write TTLs and modalities itself.
func withProviderUsage(encoded []byte, provider map[string]any) []byte {
	var out map[string]any
	if json.Unmarshal(encoded, &out) != nil {
		return encoded
	}
	out["provider_usage"] = provider
	result, err := json.Marshal(out)
	if err != nil {
		return encoded
	}
	return result
}

func withCompactionResponse(converted, raw []byte) []byte {
	var source, out map[string]any
	if json.Unmarshal(raw, &source) != nil || json.Unmarshal(converted, &out) != nil {
		return converted
	}
	var blocks []any
	if content, ok := source["content"].([]any); ok {
		for _, v := range content {
			if block, ok := v.(map[string]any); ok && block["type"] == "compaction" {
				blocks = append(blocks, block)
			}
		}
	}
	if len(blocks) == 0 {
		return converted
	}
	choices, ok := out["choices"].([]any)
	if !ok || len(choices) == 0 {
		return converted
	}
	choice, _ := choices[0].(map[string]any)
	message, _ := choice["message"].(map[string]any)
	if message == nil {
		return converted
	}
	message["provider_specific_fields"] = map[string]any{"compaction_blocks": blocks}
	encoded, err := schemas.Marshal(out)
	if err != nil {
		return converted
	}
	return encoded
}

func applyAnthropicHeaders(ctx *schemas.BifrostContext, p *executionPlan) error {
	if err := anthropic.AddMissingBetaHeadersToContextFromRawBody(ctx, p.body, schemas.Anthropic); err != nil {
		return err
	}
	values := anthropic.MergeBetaHeaders(ctx, map[string]schemas.SecretVar{"anthropic-beta": *schemas.NewSecretVar(p.headers.Get("Anthropic-Beta"))})
	if len(values) > 0 {
		p.headers.Set("Anthropic-Beta", strings.Join(values, ","))
	}
	return nil
}
