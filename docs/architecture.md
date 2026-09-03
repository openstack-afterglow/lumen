# 아키텍처

Lumen은 direct `/v1` API와 Afterglow BFF가 rewrite하는 `/api/v1/chat/*`의 실행 서버다. compat API에서 provider model ID 지정 시에는 LiteLLM completion을 stateless로 중계하며, reserved model `lumen` 지정 시에는 `chat_admission` 및 `durable_runs` admission을 통해 `chat_default_model` 기반 durable run으로 worker에서 실행된다. native API는 durable journal에 intent를 기록하고 worker가 실행한다.

```mermaid
flowchart LR
  C[SDK/OpenAI/Anthropic client] --> V[/v1]
  B[Afterglow BFF /api/v1/chat] --> V
  V --> A[FastAPI route + Principal/scope]
  A --> N[chat_admission]
  N --> R[durable_runs admission + MariaDB journal]
  R --> Q[Redis best-effort wakeup]
  Q --> W[lumen-worker]
  W --> E[durable_runs execution]
  E --> G[graph / tool_runtime]
  G --> L[LiteLLM provider]
  G --> M[Custom HTTP / MCP]
  E --> U[usage ledger]
  R --> S[SSE replay]
  N --> P[providers routing]
  M --> X[SSRF/TLS boundary]
  E --> PG[PostgreSQL checkpointer / pgvector, optional]
  E --> O[S3 assets, optional]
```

## 경계와 source of truth

- MariaDB: provider/model catalog, conversation, durable run/event journal, extension/memory metadata, API key, usage ledger의 source of truth다.
- Redis: `afterglow:chat:runs` wakeup 최적화다. 유실되어도 worker DB polling이 queued run을 발견한다.
- PostgreSQL: configured checkpointer와 semantic memory(pgvector)의 선택 경계다.
- S3/ClamAV/sandbox/MCP: asset, scan, code execution, external tool 경계이며 필수 기능이 아닌 경우 비활성화된다.
- OpenStack 결합: Keystone principal과 `lumen_sdk.register(openstack.Connection)` transport만 OpenStack SDK에 결합한다. direct `lumen_sdk.Client`는 httpx transport를 사용한다.

## 모듈 ownership

`api` → `chat_admission` → `durable_runs` → `graph`/`tool_runtime`/store/model 방향을 유지한다.

| Package | 책임 |
| --- | --- |
| `services/providers` | provider/model CRUD, pricing/capability projection, immutable execution route |
| `services/durable_runs` | admission, journal query, interaction, lease/lifecycle, worker execution |
| `services/tool_runtime` | v2 contracts, binding, frozen selection/schema, managed tool, dispatch |
| `services/chat_admission.py` | capability/feature gate, context/snapshot, API-native admission preparation |
| `services/graph.py` | LangGraph state machine과 model/tool loop |

`graph.py`, `conversation_store.py`, `chat_contracts.py`는 각각 state machine, repository, wire contract로 응집되어 있어 package facade로 분리하지 않는다.
