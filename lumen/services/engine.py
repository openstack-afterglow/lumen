"""chat_engine 인터페이스 — completions 엔드포인트가 의존하는 스트리밍 경계.

Phase 2: 실제 구현을 LangGraph 에이전트 그래프(graph.stream)에 위임한다. 이벤트 계약
(token/tool_call/usage/error)은 불변이므로 엔드포인트·테스트는 engine.stream 을 그대로 호출/패치한다.
project_id/user_id 는 툴 실행의 테넌트 컨텍스트로 전달된다(LLM 입력 아님).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from lumen.services import graph


async def stream(
    *,
    model: str,
    messages: list[dict],
    project_id: str,
    user_id: str,
    custom_llm_provider: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    selected_tool_ids: tuple[int, ...] | None = None,
    selected_mcp_ids: tuple[int, ...] | None = None,
    expected_mcp_credential_versions: tuple[tuple[int, int], ...] | None = None,
    expected_extension_fingerprints: tuple[tuple[str, int, str], ...] | None = None,
    tools_enabled: bool = True,
    managed_search: dict | None = None,
    managed_fetch: dict | None = None,
    managed_advisor: dict | None = None,
    response_format: dict | None = None,
    run_id: str | None = None,
    execution_hooks: object | None = None,
    execution_protocol_version: int = 1,
    approval_mode: str | None = None,
    workspace_write_mode: str = "ask",
    max_model_turns: int = 8,
    max_tool_calls: int = 24,
    allowed_direct_effects: tuple[str, ...] | None = None,
    lumen_snapshot: dict[str, object] | None = None,
    lumen_snapshot_frozen: bool = False,
    resume: list[dict[str, str]] | None = None,
) -> AsyncIterator[dict]:
    async for ev in graph.stream(
        model=model,
        messages=messages,
        project_id=project_id,
        user_id=user_id,
        custom_llm_provider=custom_llm_provider,
        api_base=api_base,
        api_key=api_key,
        max_tokens=max_tokens,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        selected_tool_ids=selected_tool_ids,
        selected_mcp_ids=selected_mcp_ids,
        expected_mcp_credential_versions=expected_mcp_credential_versions,
        expected_extension_fingerprints=expected_extension_fingerprints,
        tools_enabled=tools_enabled,
        managed_search=managed_search,
        managed_fetch=managed_fetch,
        managed_advisor=managed_advisor,
        run_id=run_id,
        response_format=response_format,
        execution_hooks=execution_hooks,
        execution_protocol_version=execution_protocol_version,
        approval_mode=approval_mode,
        workspace_write_mode=workspace_write_mode,
        max_model_turns=max_model_turns,
        max_tool_calls=max_tool_calls,
        allowed_direct_effects=allowed_direct_effects,
        lumen_snapshot=lumen_snapshot,
        lumen_snapshot_frozen=lumen_snapshot_frozen,
        resume=resume,
    ):
        yield ev
