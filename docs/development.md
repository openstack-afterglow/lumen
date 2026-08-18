# 개발 구조

## 최종 소유권

| 경로 | 소유 책임 |
| --- | --- |
| `api/*.py` | HTTP route, auth/scope dependency, request parse, HTTP/SSE mapping |
| `services/chat_admission.py` | native capability/context/snapshot/admission preparation |
| `services/durable_runs/{admission,interactions,queries,lifecycle,execution}.py` | durable run lifecycle |
| `services/providers/{repository,pricing,routing}.py` | provider/model state, price/capability, execution route |
| `services/tool_runtime/{contracts,bindings,selection,managed,dispatch}.py` | server tool runtime |
| `models/chat_contracts.py` | public request/response/event contracts |

의존성은 API → application service → runtime/store/model 단방향이다. 상위 계층은 service를 우회하려고 하위 계층의 private owner를 import해서는 안 된다. package `__init__.py`는 docstring만 두며 compatibility re-export를 복원하지 않는다.

## 새 capability checklist

1. `chat_contracts.py`에 public wire contract를 정의하고 검증한다.
2. least-privilege API-key scope와 admission gate를 추가한다. default를 조용히 downgrade하지 않는다.
3. priced capability/provider route를 resolve하고 admission에 non-secret immutable snapshot을 저장한다.
4. worker execution에서 mutable extension/provider configuration을 재검증한다.
5. observable lifecycle/tool/usage event를 journal에 기록하고 SSE replay는 read-only로 유지한다.
6. cost를 `source`, `api_key_id`, `run_id`에 attribution한다. public usage record는 raw cost internal을 제외한다.
7. real datastore/worker boundary 변경에는 focused unit test와 integration coverage를 추가한다.

## God-module 회귀 방지

책임을 기준으로만 코드를 이동한다. `graph.py`는 cohesive state machine, `conversation_store.py`는 conversation repository, `chat_contracts.py`는 wire-contract source를 유지한다. 또 다른 facade, helper copy, `utils` bucket을 만들지 않는다. cross-package import는 explicit owner module을 가리킨다.

## 구현됨과 선언만 존재함

| 영역 | 상태 |
| --- | --- |
| scoped API-key native durable chat, run journal/replay, usage provenance | 구현됨 |
| custom HTTP/MCP/managed tool runtime, skill/memory selection | configured provider/runtime에서 구현됨 |
| v2 approval/interaction protocol | 구현됨; checkpointer deployment 필요 |
| semantic prompt ranking, extension installer, subagent runtime, sandbox binding | runtime feature로 구현되지 않음 |
