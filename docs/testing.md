# 테스트

## 명령

```bash
# focused contract
uv run pytest tests/test_chat_api_keys.py tests/test_chat_completions.py tests/test_chat_run_protocol.py \
  tests/test_chat_tool_runtime.py tests/test_chat_extensions.py tests/test_chat_memory.py tests/test_chat_usage.py

# datastore 없는 server suite
uv run pytest -m "not integration" tests
uv run ruff check lumen tests

# SDK parity
(cd sdk && uv run pytest && uv run ruff check .)

# MariaDB + Redis, migration applied
uv run pytest -m integration
```

`crypto` marker는 encryption-domain unit test를 식별한다. `integration` marker는 MariaDB와 Redis가 필요하며 CI가 `lumen-migrate --apply` 후 실행한다.

## Fixture와 monkeypatch

단위 테스트는 store/provider/network boundary를 monkeypatch하고 실제 외부 provider, MCP, S3를 호출하지 않는다. monkeypatch target은 facade가 아니라 actual owner module이어야 한다. 예: legacy schema는 `tool_runtime.selection`, dispatch는 `tool_runtime.dispatch`, v2 binding은 `tool_runtime.bindings`다.

## 변경별 최소 coverage

| 변경 | 필수 검증 |
| --- | --- |
| auth/API key/scope | `test_chat_api_keys.py`, completion/usage tests |
| durable journal/worker | `test_chat_run_protocol.py`, worker/replay behavior |
| tool runtime | `test_chat_tool_runtime.py`, `test_chat_agent_protocol.py`, graph tests |
| extension/skill | extension store/API/skill tests |
| memory | memory scope, extraction, semantic-memory tests |
| provider pricing/routing | provider/admin and run admission tests |
| SDK route transport | `sdk/tests/test_proxy.py`와 direct-client parity tests |
| migration | empty DB와 pre-migration schema에서 apply + second no-op |

## Integration environment

CI `Native API integration` job은 MariaDB 11, Redis 7, `DATABASE_URL`, `REDIS_URL`, `LUMEN_ENCRYPTION_KEY`를 제공한다. `tests/integration/test_native_api_key_flow.py`는 API-key HTTP admission, database run journal, worker execution, SSE replay와 API-key usage record attribution을 검증하고 LiteLLM network를 호출하지 않는다.

pgvector test는 PostgreSQL/pgvector와 matching `chat_memory_*` settings가 있을 때만 추가한다. 테스트가 shared datastore를 쓸 경우 unique user/project/model IDs를 만들고 owner-scoped data만 정리한다.
