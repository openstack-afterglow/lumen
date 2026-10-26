package bifrost

import "github.com/maximhq/bifrost/core/schemas"

// The pinned neutral mux subtracts one from positive output indexes, assuming a
// reasoning item precedes calls. Provider output indexes also include text and
// other native items, so neither subtraction nor direct reuse identifies a chat
// tool delta. Allocate contiguous indexes only for function calls, per request.
type responseToolIndexes struct {
	byOutput map[int]uint16
	next     int
}

func (state *responseToolIndexes) apply(event *schemas.BifrostResponsesStreamResponse, chat *schemas.BifrostChatResponse) error {
	functionCall := event.Type == schemas.ResponsesStreamResponseTypeFunctionCallArgumentsDelta
	if event.Type == schemas.ResponsesStreamResponseTypeOutputItemAdded && event.Item != nil && event.Item.Type != nil && *event.Item.Type == schemas.ResponsesMessageTypeFunctionCall {
		functionCall = true
	}
	if !functionCall {
		return nil
	}
	if event.OutputIndex == nil || *event.OutputIndex < 0 {
		return errUnsupported
	}
	output := *event.OutputIndex
	index, exists := state.byOutput[output]
	if !exists {
		if state.next > 65535 {
			return errUnsupported
		}
		index = uint16(state.next)
		state.next++
		state.byOutput[output] = index
	}
	if chat == nil {
		return nil
	}
	for choiceIndex := range chat.Choices {
		choice := &chat.Choices[choiceIndex]
		if choice.ChatStreamResponseChoice == nil || choice.ChatStreamResponseChoice.Delta == nil {
			continue
		}
		for callIndex := range choice.ChatStreamResponseChoice.Delta.ToolCalls {
			choice.ChatStreamResponseChoice.Delta.ToolCalls[callIndex].Index = index
		}
	}
	return nil
}
