## Context

Lumen의 OpenAI 호환 route는 현재 `completion_api`를 통해 provider를 동기/스트리밍 직접 호출한다. Native route는 admission이 intent와 실행 snapshot을 MariaDB에 기록하고 worker가 `engine.stream`/LangGraph를 실행하며, journal event를 통해 결과와 사용량을 전달한다. 이번 변경은 OpenAI SDK 표면을 유지하면서 `model="lumen"`만 native durable 실행 경계로 연결하는 첫 단계다.

## Goals / Non-Goals

**Goals:**
- OpenAI SDK가 `model="lumen"`으로 non-stream/stream chat completion을 호출할 수 있다.
- 요청은 API process가 provider를 직접 호출하지 않고 durable run을 생성한 뒤 worker journal을 소비한다.
- 내부 provider는 `chat_default_model`로 서버가 선택하고 외부 응답에는 virtual model id만 노출한다.
- API key quota/usage attribution, host gate, tenant ownership, timeout/disconnect 취소를 보존한다.
- 기존 provider model direct compatibility와 Anthropic endpoint를 회귀 없이 유지한다.

**Non-Goals:**
- OpenAI Responses API, embeddings, audio/image, batch, fine-tuning 호환.
- caller-defined OpenAI tools/tool calls 또는 Lumen extension/MCP/memory 실행.
- OpenAI 요청에서 Lumen agent/skill/workspace를 선택하는 기능.
- Anthropic endpoint를 durable Lumen virtual model로 전환하는 작업.

## Decisions

1. **예약 virtual model id `lumen`을 추가한다.**
   - `/v1/models`는 active `chat_default_model`이 존재할 때 `owned_by="lumen"`인 `lumen` 항목을 provider model 목록 앞에 반환한다.
   - `model="lumen"`은 durable adapter로 분기하고 그 외 model id는 기존 direct completion 경로를 유지한다.
   - 기존 경로를 제거하는 대신 additive 분기를 택해 현재 사용자와 Anthropic 호환성을 깨지 않는다.

2. **OpenAI transcript를 text-only native run input으로 정규화한다.**
   - `system`/`developer`/`user`/`assistant` 문자열 content만 허용하고 `developer`는 provider 호환을 위해 system 역할로 정규화한다.
   - 마지막 message는 user여야 하며 message 수와 전체 text 크기를 제한한다.
   - multimodal content, tool role, caller `tools`/`tool_choice`는 400 OpenAI error로 fail closed한다.
   - Lumen feature snapshot은 `memory=false`, `tool_policy.mode="none"`, text output만 사용한다. 첫 단계에서 compat scope가 server-side extension/tool 권한으로 승격되지 않도록 한다.

3. **새 adapter service가 native admission과 journal replay를 재사용한다.**
   - `chat_default_model`을 provider routing에서 resolve하고 기존 credit precheck, capability/pricing snapshot, `durable_runs.admission.create_temp_run`을 사용한다.
   - OpenAI transcript 전체를 `request_payload.input_messages`로 저장하고 마지막 user text를 canonical `input_parts`로 저장한다.
   - 응답 ID는 durable run ID에서 파생해 운영 추적성을 유지한다.
   - 별도 execution engine 또는 API-process provider 호출 경로를 만들지 않는다.

4. **journal event를 OpenAI 응답으로 투영한다.**
   - `part.delta(text)`를 content chunk로, `usage.updated`를 usage로, terminal event를 finish/error로 변환한다.
   - non-stream은 terminal까지 누적하고, stream은 assistant role chunk부터 text chunks, finish chunk, 선택적 usage chunk, `[DONE]` 순서로 보낸다.
   - server timeout은 bounded 설정값을 사용한다. timeout, client disconnect, generator cancellation 시 nonterminal run에 cancel을 요청해 orphan execution을 남기지 않는다.

5. **OpenAI route 오류는 top-level error body를 반환한다.**
   - admission/validation/provider/run failure는 `JSONResponse({"error": ...})`를 사용한다.
   - 기존 FastAPI `HTTPException(detail={"error": ...})`의 추가 `detail` wrapper는 OpenAI SDK 오류 계약과 맞지 않으므로 이 route에서 제거한다.

6. **설정과 배포를 같은 릴리스에 연결한다.**
   - `chat_default_model`과 compat run timeout을 settings/example/Kolla template에 반영한다.
   - standalone Compose는 `LUMEN_LOCAL_MODEL`을 API/worker의 `CHAT_DEFAULT_MODEL`로 전달한다.
   - Kolla 기본값은 비어 있게 두어 기존 배포를 깨지 않고, 운영자가 active provider model을 명시한 경우에만 virtual model을 advertise한다.

## Risks / Trade-offs

- [Worker가 없으면 HTTP 요청이 timeout까지 대기] → bounded timeout과 run cancel, system test로 API+worker 경계를 검증한다.
- [한 HTTP stream이 DB journal을 polling] → native SSE와 같은 owner-scoped query를 사용하고 짧은 interval, terminal 즉시 종료를 적용한다.
- [OpenAI message 기능 일부 미지원] → 지원하지 않는 tool/multimodal 입력을 조용히 무시하지 않고 명시적 400으로 반환한다.
- [virtual model과 provider model 이름 충돌] → `lumen`을 예약하고 해당 id는 항상 virtual route가 우선한다.
- [compat scope가 native 권한을 우회] → memory/tool/extension selection을 고정 비활성화하고 owner/project/api_key attribution만 native run에 전달한다.

## Migration Plan

1. additive 코드/설정과 tests를 배포한다.
2. 운영 Lumen에 active provider model과 동일한 `lumen_chat_default_model`을 설정한다.
3. `/v1/models`에서 `lumen` 노출을 확인한 뒤 OpenAI non-stream/stream smoke test를 수행한다.
4. 문제 발생 시 설정을 비워 virtual model만 숨기고 기존 provider direct/Anthropic/native API를 그대로 유지한다.

## Open Questions

없음. 추가 virtual agent/model, server-side tool/memory 기능, Anthropic durable adapter는 별도 OpenSpec change로 순차 도입한다.
