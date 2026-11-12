package bifrost

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/aws/protocol/eventstream"
	"github.com/aws/aws-sdk-go-v2/aws/signer/v4"
	"github.com/maximhq/bifrost/core/providers/anthropic"
	"github.com/maximhq/bifrost/core/providers/bedrock"
	"github.com/maximhq/bifrost/core/providers/openai"
	"github.com/maximhq/bifrost/core/schemas"
)

// Cloud credentials are explicit request-local data, never SDK environment/default chains.
// A Bedrock key may be a bearer token, or JSON with region/access_key_id/secret_access_key
// and optional session_token. Vertex accepts a bearer token with an administrator-supplied
// full publisher base; service-account refresh belongs to Lumen's credential resolver.
type awsCredential struct {
	Region          string `json:"region"`
	AccessKeyID     string `json:"access_key_id"`
	SecretAccessKey string `json:"secret_access_key"`
	SessionToken    string `json:"session_token"`
}

func prepareCloud(ctx *schemas.BifrostContext, cancel context.CancelFunc, e Envelope, model string, stream bool, headers http.Header) (*executionPlan, error) {
	if e.Provider != "bedrock" {
		return nil, errUnsupported
	}
	var credential awsCredential
	if json.Unmarshal([]byte(e.APIKey), &credential) != nil || credential.Region == "" || strings.ContainsAny(credential.Region, "/.:") {
		return nil, errUnsupported
	}
	base := "https://bedrock-runtime." + credential.Region + ".amazonaws.com"
	return prepareCloudAtBase(ctx, cancel, e, base, model, stream, headers)
}
func prepareCloudAtBase(ctx *schemas.BifrostContext, cancel context.CancelFunc, e Envelope, base, model string, stream bool, headers http.Header) (*executionPlan, error) {
	if _, err := validBase(base); err != nil {
		return nil, err
	}
	if e.Protocol == "embeddings" {
		return prepareBedrockEmbedding(ctx, cancel, e, base, model, headers)
	}
	var request *schemas.BifrostResponsesRequest
	if e.Protocol == "chat" {
		var wire openai.OpenAIChatRequest
		if schemas.Unmarshal(e.Body, &wire) != nil {
			return nil, errUnsupported
		}
		chat := wire.ToBifrostChatRequest(ctx)
		chat.Model = model
		chat.Provider = schemas.Bedrock
		request = chat.ToResponsesRequest()
	} else if e.Protocol == "responses" {
		var wire openai.OpenAIResponsesRequest
		if schemas.Unmarshal(e.Body, &wire) != nil {
			return nil, errUnsupported
		}
		request = wire.ToBifrostResponsesRequest(ctx)
		request.Model = model
		request.Provider = schemas.Bedrock
	} else if e.Protocol == "messages" {
		var wire anthropic.AnthropicMessageRequest
		if schemas.Unmarshal(e.Body, &wire) != nil {
			return nil, errUnsupported
		}
		request = wire.ToBifrostResponsesRequest(ctx)
		request.Model = model
		request.Provider = schemas.Bedrock
	} else {
		return nil, errUnsupported
	}
	wire, err := bedrock.ToBedrockResponsesRequest(ctx, request)
	if err != nil {
		return nil, err
	}
	raw, err := schemas.Marshal(wire)
	if err != nil {
		return nil, err
	}
	operation := "converse"
	if stream {
		operation = "converse-stream"
	}
	p := &executionPlan{body: raw, headers: headers, target: appendPath(base, "model/"+url.PathEscape(model)+"/"+operation), stream: stream, kind: e.Protocol, close: cancel}
	if err = signCloudPlan(ctx, p, e.APIKey); err != nil {
		return nil, err
	}
	p.response = func(raw []byte) ([]byte, error) {
		var native bedrock.BedrockConverseResponse
		if schemas.Unmarshal(raw, &native) != nil {
			return nil, errUnsupported
		}
		response, err := native.ToBifrostResponsesResponse(ctx)
		if err != nil {
			return nil, err
		}
		if e.Protocol == "responses" {
			return schemas.Marshal(response)
		}
		if e.Protocol == "messages" {
			return schemas.Marshal(anthropic.ToAnthropicResponsesResponse(ctx, response))
		}
		out, err := schemas.Marshal(response.ToBifrostChatResponse())
		if err != nil {
			return nil, err
		}
		return preserveMetadata(out, raw, "bedrock"), nil
	}
	state := bedrock.NewBedrockStreamStateWithContext(ctx)
	responsesState := bedrock.NewBedrockResponsesStreamState()
	sequence := 0
	p.event = func(raw []byte) ([][]byte, bool, error) {
		var event bedrock.BedrockStreamEvent
		if schemas.Unmarshal(raw, &event) != nil {
			return nil, false, errUnsupported
		}
		var result [][]byte
		terminal := event.StopReason != nil
		if e.Protocol == "responses" || e.Protocol == "messages" {
			converted, bErr, _ := event.ToBifrostResponsesStream(sequence, responsesState)
			if bErr != nil {
				return nil, false, errUnsupported
			}
			sequence += len(converted)
			for _, v := range converted {
				if e.Protocol == "messages" {
					for _, native := range anthropic.ToAnthropicResponsesStreamResponse(ctx, v) {
						b, err := schemas.Marshal(native)
						if err != nil {
							return nil, false, err
						}
						result = append(result, b)
					}
				} else {
					b, err := schemas.Marshal(v)
					if err != nil {
						return nil, false, err
					}
					result = append(result, b)
				}
			}
		} else {
			converted, bErr, _ := event.ToBifrostChatCompletionStream(state)
			if bErr != nil {
				return nil, false, errUnsupported
			}
			if converted != nil {
				b, err := schemas.Marshal(converted)
				if err != nil {
					return nil, false, err
				}
				result = append(result, b)
			}
		}
		return result, terminal, nil
	}
	p.wrapStream = bedrockSSE
	return p, nil
}

func bedrockSSE(src io.Reader) io.ReadCloser {
	reader, writer := io.Pipe()
	go func() {
		decoder := eventstream.NewDecoder()
		for {
			message, err := decoder.Decode(src, nil)
			if err != nil {
				if err == io.EOF {
					_ = writer.Close()
				} else {
					_ = writer.CloseWithError(err)
				}
				return
			}
			if header := message.Headers.Get(":message-type"); header != nil && header.String() != "event" {
				_ = writer.CloseWithError(errUnsupported)
				return
			}
			if !json.Valid(message.Payload) {
				_ = writer.CloseWithError(errUnsupported)
				return
			}
			if _, err = io.Copy(writer, io.MultiReader(bytes.NewReader([]byte("data: ")), bytes.NewReader(message.Payload), strings.NewReader("\n\n"))); err != nil {
				return
			}
		}
	}()
	return reader
}

func signCloudPlan(ctx context.Context, p *executionPlan, key string) error {
	var credential awsCredential
	if strings.HasPrefix(strings.TrimSpace(key), "{") {
		if json.Unmarshal([]byte(key), &credential) != nil || credential.AccessKeyID == "" || credential.SecretAccessKey == "" || credential.Region == "" {
			return errUnsupported
		}
		p.headers.Del("Authorization")
		p.sign = func(req *http.Request) error {
			sum := sha256.Sum256(p.body)
			return v4.NewSigner().SignHTTP(ctx, aws.Credentials{AccessKeyID: credential.AccessKeyID, SecretAccessKey: credential.SecretAccessKey, SessionToken: credential.SessionToken}, req, hex.EncodeToString(sum[:]), "bedrock", credential.Region, time.Now())
		}
	} else if p.headers.Get("Authorization") == "" {
		p.headers.Set("Authorization", "Bearer "+key)
	}
	return nil
}

func prepareBedrockEmbedding(ctx *schemas.BifrostContext, cancel context.CancelFunc, e Envelope, base, model string, headers http.Header) (*executionPlan, error) {
	var native openai.OpenAIEmbeddingRequest
	if schemas.Unmarshal(e.Body, &native) != nil {
		return nil, errUnsupported
	}
	request := native.ToBifrostEmbeddingRequest(ctx)
	request.Model = model
	request.Provider = schemas.Bedrock
	family, err := bedrock.DetermineEmbeddingModelType(ctx, model)
	if err != nil {
		return nil, err
	}
	var wire any
	switch family {
	case "titan":
		wire, err = bedrock.ToBedrockTitanEmbeddingRequest(request)
	case "cohere":
		wire, err = bedrock.ToBedrockCohereEmbeddingRequest(request)
	case "nova":
		wire, err = bedrock.ToBedrockNovaEmbeddingRequest(request)
	default:
		return nil, errUnsupported
	}
	if err != nil {
		return nil, err
	}
	raw, err := schemas.Marshal(wire)
	if err != nil {
		return nil, err
	}
	p := &executionPlan{body: raw, headers: headers, target: appendPath(base, "model/"+url.PathEscape(model)+"/invoke"), close: cancel}
	if err = signCloudPlan(ctx, p, e.APIKey); err != nil {
		return nil, err
	}
	p.response = func(raw []byte) ([]byte, error) {
		var response *schemas.BifrostEmbeddingResponse
		switch family {
		case "titan":
			var native bedrock.BedrockTitanEmbeddingResponse
			if schemas.Unmarshal(raw, &native) != nil {
				return nil, errUnsupported
			}
			response = native.ToBifrostEmbeddingResponse()
		case "cohere":
			var native bedrock.BedrockCohereEmbeddingResponse
			if schemas.Unmarshal(raw, &native) != nil {
				return nil, errUnsupported
			}
			response, err = native.ToBifrostEmbeddingResponse()
		case "nova":
			var native bedrock.BedrockNovaEmbeddingResponse
			if schemas.Unmarshal(raw, &native) != nil {
				return nil, errUnsupported
			}
			response, err = native.ToBifrostEmbeddingResponse()
		}
		if err != nil || response == nil {
			return nil, errUnsupported
		}
		response.Model = model
		return schemas.Marshal(response)
	}
	return p, nil
}
