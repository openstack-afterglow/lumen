# Direct OpenAI Responses for chat-shaped calls

The native `chat_conversation.completions` event names the Lumen action that admitted a durable run; it does not specify the upstream provider API. HTTP 202 is admission, not proof of model completion. The existing graph calls LiteLLM chat completions for official OpenAI models, whereas the stateless `/v1/responses` proxy already calls Responses.

Route chat-shaped calls for official OpenAI endpoints through LiteLLM's installed Responses-to-Chat translation bridge. Keep the graph's normalized token/tool/usage contract, frozen run boundaries and API surface unchanged. Do not redirect third-party OpenAI-compatible URLs or subscription routes. Do not silently retry Chat Completions after a Responses failure; the result of an indeterminate request must not be duplicated. This is a transport preference, not a verified diagnosis of the reported production failure.

Acceptance: the installed LiteLLM HTTP client targets `/v1/responses` on a local intercepted fake transport for official direct OpenAI, text and tool/usage continuations survive, compatible custom endpoints still target Chat Completions, and documentation distinguishes local proof from a production/provider result.
