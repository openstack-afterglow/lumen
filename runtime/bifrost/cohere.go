package bifrost

import (
	"context"
	"net/http"
	"strings"

	"github.com/maximhq/bifrost/core/providers/cohere"
	"github.com/maximhq/bifrost/core/providers/openai"
	"github.com/maximhq/bifrost/core/schemas"
)

func cohereBase(base string) string {
	if strings.HasSuffix(base, "/v1") {
		return strings.TrimSuffix(base, "/v1") + "/v2"
	}
	if strings.HasSuffix(base, "/v2") {
		return base
	}
	return strings.TrimRight(base, "/") + "/v2"
}
func prepareCohereEmbedding(ctx *schemas.BifrostContext, cancel context.CancelFunc, e Envelope, base, model string, headers http.Header) (*executionPlan, error) {
	var native openai.OpenAIEmbeddingRequest
	if schemas.Unmarshal(e.Body, &native) != nil {
		return nil, errUnsupported
	}
	request := native.ToBifrostEmbeddingRequest(ctx)
	request.Model = model
	request.Provider = schemas.Cohere
	wire, err := cohere.ToCohereEmbeddingRequest(request)
	if err != nil {
		return nil, err
	}
	raw, err := schemas.Marshal(wire)
	if err != nil {
		return nil, err
	}
	p := &executionPlan{body: raw, headers: headers, target: appendPath(cohereBase(base), "embed"), close: cancel}
	p.response = func(raw []byte) ([]byte, error) {
		var native cohere.CohereEmbeddingResponse
		if schemas.Unmarshal(raw, &native) != nil {
			return nil, errUnsupported
		}
		converted := native.ToBifrostEmbeddingResponse()
		if converted == nil {
			return nil, errUnsupported
		}
		return schemas.Marshal(converted)
	}
	return p, nil
}
