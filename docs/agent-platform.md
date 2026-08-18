# 에이전트 플랫폼

## 도구 종류와 실행

내장 도구 literal은 `list_my_conversations`, `get_conversation_detail` 두 개다. `tool_runtime`은 다음 경계를 유지한다.

- `contracts`: v2 binding/result/effect/config fingerprint.
- `bindings`: builtin, Lumen registry, custom HTTP, MCP의 executable binding과 deferred restore.
- `selection`: frozen extension selection 재검증, legacy schema, activity metadata.
- `managed`: managed web search/fetch/advisor quota·schema·result.
- `dispatch`: bounded custom HTTP와 MCP 실행, legacy execute entrypoint.

Custom HTTP는 SSRF-safe transport, private-address block, redirect 미추적, identity encoding, bounded response를 사용한다. MCP는 HTTPS HTTP transport와 owner/project selection을 재검증한다. OAuth connection은 server-side secret으로 합쳐지고 OAuth start/callback은 browser Keystone flow다.

## Skill과 컨텍스트

Skill instruction은 encrypted extension storage에서 owner/project selection으로 읽고 run snapshot에 고정한다. Admission은 agent/request skill IDs, extension selection, model route, memory/workspace context를 materialize한다. Worker는 frozen extension configuration을 다시 검증한다. Context는 caller text보다 서버가 소유한 system/context instruction을 우선하는 순서로 구성한다.

## 메모리

Memory CRUD는 account/project/workspace scope를 가진다. API key는 자기 project/workspace memory만 다루며 account scope를 쓸 수 없다. `features.memory=true` run은 prompt hydration과 완료 뒤 project-memory extraction/mutation job을 모두 의미하므로 read+write scope가 필요하다. Semantic search API와 recency-based prompt hydration은 별도이며, semantic ranking을 prompt에 자동 결합하지 않는다.

## 승인과 protocol

v1은 legacy model/tool loop이며 v2는 binding protocol, effect policy, approval/interaction resume를 쓴다. v2는 encrypted PostgreSQL checkpointer가 필요하다. Durable run journal은 tool call started/completed, part delta/completed, usage, terminal event를 저장해 SSE가 replay한다.

## 구현 상태

extension package installer, subagent spawn, sandbox binding, semantic prompt ranking, title-summary caller는 현재 실행 runtime으로 구현되지 않았다. 관련 schema/policy/client surface가 존재해도 실행 기능으로 문서화하지 않는다.
