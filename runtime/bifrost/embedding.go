package bifrost

import (
	"context"
	"net/http"
	"net/url"
	"strings"

	"github.com/maximhq/bifrost/core/providers/gemini"
	"github.com/maximhq/bifrost/core/providers/openai"
	"github.com/maximhq/bifrost/core/schemas"
)

func prepareEmbedding(ctx *schemas.BifrostContext, cancel context.CancelFunc, e Envelope, base, model string, headers http.Header) (*executionPlan, error) {
	if e.Provider != "gemini" && e.Provider != "vertex" {
		return nil, errUnsupported
	}
	var native openai.OpenAIEmbeddingRequest
	if schemas.Unmarshal(e.Body, &native) != nil {
		return nil, errUnsupported
	}
	request := native.ToBifrostEmbeddingRequest(ctx)
	request.Model = model
	request.Provider = schemas.ModelProvider(e.Provider)
	wire, err := gemini.ToGeminiEmbeddingRequest(request)
	if err != nil {
		return nil, err
	}
	raw, err := schemas.Marshal(wire)
	if err != nil {
		return nil, err
	}
	p := &executionPlan{body: raw, headers: headers, target: appendPath(base, "models/"+url.PathEscape(strings.TrimPrefix(model, "models/"))+":batchEmbedContents"), close: cancel}
	p.response = func(raw []byte) ([]byte, error) {
		var native gemini.GeminiEmbeddingResponse
		if schemas.Unmarshal(raw, &native) != nil {
			return nil, errUnsupported
		}
		converted := gemini.ToBifrostEmbeddingResponse(&native, model)
		if converted == nil {
			return nil, errUnsupported
		}
		return schemas.Marshal(converted)
	}
	return p, nil
}
