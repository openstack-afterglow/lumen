import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from lumen.api import completions
from lumen.config import get_settings
from lumen.models.chat_agent_platform import ChatCodeWorkspace, ChatCommand, ChatRunInteraction
from lumen.models.chat_contracts import validate_chat_run_event
from lumen.models.chat_db import ChatUsageLog
from lumen.models.chat_runs import ChatRun, ChatToolApproval
from lumen.services import context_manager, execution_protocol, run_store
from lumen.services.durable_runs import common, execution, interactions, lifecycle, queries
from lumen.services.durable_runs import errors as durable_errors
from lumen.services.durable_runs.common import _fingerprint
from lumen.services.run_protocol_v2 import transition_allowed


def test_idempotency_fingerprint_normalizes_feature_domain_order():
    base = {
        "endpoint": "completion",
        "features": {
            "web_search": {"allowed_domains": ["b.example", "a.example"], "blocked_domains": []},
            "web_fetch": {"allowed_domains": [], "blocked_domains": ["z.example", "y.example"]},
        },
    }
    reordered = {
        "endpoint": "completion",
        "features": {
            "web_search": {"allowed_domains": ["a.example", "b.example"], "blocked_domains": []},
            "web_fetch": {"allowed_domains": [], "blocked_domains": ["y.example", "z.example"]},
        },
    }
    assert _fingerprint(base) == _fingerprint(reordered)


def test_v2_run_protocol_models_preserve_v1_default_and_additive_waiting_state_storage():
    assert ChatRun.__table__.c.execution_protocol_version.default.arg == 1
    assert {"execution_mode", "parent_run_id", "root_run_id", "policy_snapshot"} <= set(ChatRun.__table__.c.keys())
    assert {
        "arguments_ciphertext",
        "dispatch_hmac",
        "preview_fingerprint",
        "decision_hmac",
        "expected_state_revision",
        "writer_fence",
    } <= set(ChatToolApproval.__table__.c.keys())
    assert {"request_ciphertext", "response_ciphertext", "response_schema"} <= set(
        ChatRunInteraction.__table__.c.keys()
    )
    assert ChatCodeWorkspace.__table__.c.writer_fence.default.arg == 0
    assert {"project_id", "prompt_template_ciphertext", "execution_mode"} <= set(ChatCommand.__table__.c.keys())

    assert "idx_chat_tool_approvals_pending_expiry" in {index.name for index in ChatToolApproval.__table__.indexes}


def test_summary_usage_schema_allows_multiple_segments_per_run():
    assert ChatUsageLog.__table__.c.run_id.unique is not True
    assert "idx_chat_usage_run_id" in {index.name for index in ChatUsageLog.__table__.indexes}


def test_v2_approval_hmac_binds_owner_preview_expiry_and_deciding_actor():
    expires_at = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    dispatch = interactions._approval_dispatch_hmac(
        run_id="run-1",
        owner_user_id="user-1",
        project_id="project-1",
        call_id="call-1",
        name="afterglow_vm_delete",
        arguments={"server_id": "server-1"},
        source="managed",
        effect="external_mutation",
        tool_definition_hash="a" * 64,
        config_fingerprint="b" * 64,
        destination_origin=None,
        preview_fingerprint="c" * 64,
        expires_at=expires_at,
    )
    assert dispatch != interactions._approval_dispatch_hmac(
        run_id="run-1",
        owner_user_id="user-2",
        project_id="project-1",
        call_id="call-1",
        name="afterglow_vm_delete",
        arguments={"server_id": "server-1"},
        source="managed",
        effect="external_mutation",
        tool_definition_hash="a" * 64,
        config_fingerprint="b" * 64,
        destination_origin=None,
        preview_fingerprint="d" * 64,
        expires_at=expires_at + timedelta(minutes=1),
    )
    decision = interactions._approval_decision_hmac(
        dispatch_hmac=dispatch,
        owner_user_id="user-1",
        project_id="project-1",
        call_id="call-1",
        decision="approve",
        decided_by_user_id="user-1",
        decided_at=expires_at - timedelta(minutes=1),
    )
    assert decision != interactions._approval_decision_hmac(
        dispatch_hmac=dispatch,
        owner_user_id="user-1",
        project_id="project-1",
        call_id="call-1",
        decision="approve",
        decided_by_user_id="user-2",
        decided_at=expires_at - timedelta(minutes=1),
    )


def test_v2_protocol_is_worker_supported_but_requires_explicit_config_enablement():
    assert execution_protocol.is_supported(2)
    assert not execution_protocol.v2_runtime_ready(1)
    assert execution_protocol.v2_runtime_ready(2)


def test_v2_protocol_admission_requires_the_encrypted_checkpointer(monkeypatch):
    monkeypatch.setattr(common, "chat_checkpointer", SimpleNamespace(available=False))
    with pytest.raises(durable_errors.DurableRunInputError, match="encrypted PostgreSQL checkpointer"):
        common._require_supported_execution_protocol_version(2)


async def test_v2_run_admission_freezes_lumen_selection_and_overwrites_client_payload(monkeypatch):
    from lumen.services import mcp_adapter as mcp_lumen

    snapshot = mcp_lumen.LumenSnapshot(
        grant_id="grant-opaque",
        user_id="user-1",
        project_id="project-1",
        credential_epoch=7,
        selection_generation=11,
    )

    async def selected(**kwargs):
        assert kwargs == {"user_id": "user-1", "project_id": "project-1"}
        return snapshot

    monkeypatch.setattr(mcp_lumen, "selected_lumen_snapshot", selected)
    payload = await common._freeze_v2_lumen_snapshot(
        {"lumen_snapshot": {"client": "forbidden"}},
        user_id="user-1",
        project_id="project-1",
        execution_protocol_version=2,
    )

    assert payload["lumen_snapshot"] == mcp_lumen.snapshot_payload(snapshot)


def test_v2_run_snapshots_freeze_model_and_tool_limits():
    payload = common._freeze_v2_tool_call_limit({}, 2)

    assert payload["v2_max_model_turns"] == 8
    assert payload["v2_max_tool_calls"] == 24


def test_v2_run_snapshot_freezes_validated_agent_policy_limits():
    payload = common._freeze_v2_tool_call_limit(
        {
            "execution_policy": {
                "allowed_modes": ["chat"],
                "can_delegate_read": False,
                "can_delegate_write": False,
                "max_model_turns": 3,
                "max_tool_calls": 7,
                "max_children": 0,
                "max_parallel_children": 0,
                "max_child_depth": 0,
            }
        },
        2,
    )

    assert payload["v2_max_model_turns"] == 3
    assert payload["v2_max_tool_calls"] == 7
    assert payload["execution_policy"]["allowed_modes"] == ["chat"]


def test_v1_run_payload_does_not_gain_v2_execution_policy_limits():
    payload = {"execution_policy": {"max_model_turns": 1, "max_tool_calls": 1}}

    assert common._freeze_v2_tool_call_limit(payload, 1) is payload
    assert "v2_max_model_turns" not in payload
    assert "v2_max_tool_calls" not in payload


def test_v2_tool_result_projects_bounded_display_and_artifact_parts():
    parts = common._tool_display_parts(
        {
            "display": [{"type": "code", "code": "print('ok')", "language": "python"}],
            "artifacts": [
                {
                    "asset_id": "asset-1",
                    "kind": "report",
                    "name": "result.txt",
                    "media_type": "text/plain",
                    "size_bytes": 12,
                    "sha256": "a" * 64,
                }
            ],
        },
        content="model summary",
        visible=True,
    )

    assert parts == [
        {"type": "code", "code": "print('ok')", "language": "python"},
        {
            "type": "file",
            "asset_id": "asset-1",
            "mime_type": "text/plain",
            "name": "result.txt",
            "size_bytes": 12,
        },
    ]


def test_legacy_approval_event_replay_gets_required_provenance():
    row = SimpleNamespace(event_type="tool.approval_resolved", created_at=datetime(2026, 7, 26, tzinfo=UTC))

    assert run_store._normalize_replayed_payload(row, {"call_id": "call-1", "decision": "deny"}) == {
        "call_id": "call-1",
        "decision": "deny",
        "decided_by_user_id": None,
        "decided_at": "2026-07-26T00:00:00+00:00",
    }


def test_protocol_payload_check_preserves_legacy_v1_and_rejects_mismatches(monkeypatch):
    v1_run = SimpleNamespace(execution_protocol_version=1)
    v2_run = SimpleNamespace(execution_protocol_version=2)

    assert common._validate_run_protocol_payload(v1_run, {})
    assert common._validate_run_protocol_payload(v1_run, {"execution_protocol_version": 1})
    assert not common._validate_run_protocol_payload(v1_run, {"execution_protocol_version": 2})
    assert not common._validate_run_protocol_payload(v2_run, {})
    monkeypatch.setattr(common, "is_supported", lambda _version: False)
    assert not common._validate_run_protocol_payload(v1_run, {})


def test_frozen_run_forwards_only_selected_mcp_credential_versions():
    snapshot = {
        "mcp": [
            {"id": 7, "credential_version": 3},
            {"id": 8, "credential_version": 4},
            {"id": "invalid", "credential_version": 5},
        ]
    }

    assert common._selected_mcp_credential_versions(snapshot, (8,)) == ((8, 4),)


def test_managed_usage_components_use_frozen_component_prices():

    cost, components = execution._managed_usage_components(
        [
            {
                "kind": "web_search_requests",
                "price_key": "web_search_request_per_unit",
                "quantity": "1",
                "unit": "request",
                "source": "search",
            },
            {
                "kind": "web_fetch_context",
                "price_key": "web_fetch_context_per_unit",
                "quantity": "1",
                "unit": "context",
                "source": "fetch",
            },
        ],
        pricing_snapshot={
            "component_prices": {
                "web_search_request_per_unit": "0.002",
                "web_fetch_context_per_unit": "0.003",
            }
        },
        model_name="executor",
    )

    assert cost == Decimal("0.0050000000")
    assert [component["cost_usd"] for component in components] == ["0.0020000000", "0.0030000000"]


def test_v2_run_transition_table_preserves_waiting_states():
    assert transition_allowed("queued", "running")
    assert transition_allowed("running", "awaiting_input")
    assert transition_allowed("awaiting_input", "queued")
    assert transition_allowed("waiting_children", "queued")
    assert transition_allowed("awaiting_input", "finalizing", cancel_or_failure=True)
    assert not transition_allowed("completed", "queued")
    assert not transition_allowed("running", "completed")


def test_advisor_usage_uses_its_immutable_route_price():
    cost, components = execution._managed_usage_components(
        [
            {
                "kind": "advisor_input_tokens",
                "price_key": "advisor_input_price_per_token",
                "quantity": "10",
                "unit": "token",
                "source": "advisor",
                "model_name": "advisor-model",
            },
            {
                "kind": "advisor_output_tokens",
                "price_key": "advisor_output_price_per_token",
                "quantity": "5",
                "unit": "token",
                "source": "advisor",
                "model_name": "advisor-model",
            },
        ],
        pricing_snapshot={
            "component_prices": {
                "advisor_input_price_per_token": "0.0001",
                "advisor_output_price_per_token": "0.0002",
            }
        },
        model_name="executor",
    )

    assert cost == Decimal("0.0020000000")
    assert [component["model_name"] for component in components] == ["advisor-model", "advisor-model"]


async def test_completion_and_event_preflight_allow_durable_headers(client):
    origin = get_settings().cors_origin_list[0]
    for path, requested_header in (
        ("/api/v1/chat/conversations/c1/completions", "Idempotency-Key"),
        ("/api/v1/chat/runs/run-1/events", "Last-Event-ID"),
    ):
        response = await client.options(
            path,
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST" if "completions" in path else "GET",
                "Access-Control-Request-Headers": requested_header,
            },
        )
        assert response.status_code == 200
        assert requested_header.lower() in response.headers["Access-Control-Allow-Headers"].lower()


async def test_events_replay_canonical_journal_with_sse_id(client, monkeypatch):
    event = validate_chat_run_event(
        {
            "event_id": "run-1:1",
            "run_id": "run-1",
            "seq": 1,
            "type": "run.completed",
            "created_at": "2026-07-21T00:00:00Z",
            "payload": {"status": "completed", "message_id": "42"},
        }
    )
    calls = 0

    async def events(**kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["after_seq"] == 0
        return [event], True

    monkeypatch.setattr(queries, "owned_events", events)
    response = await client.get("/api/v1/chat/runs/run-1/events")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert "id: run-1:1" in response.text
    assert "event: run.completed" in response.text
    assert '"message_id": "42"' in response.text
    assert calls == 1


async def test_events_poll_at_100ms_and_keepalive_on_monotonic_ten_second_boundary(client, monkeypatch):
    terminal_event = validate_chat_run_event(
        {
            "event_id": "run-1:1",
            "run_id": "run-1",
            "seq": 1,
            "type": "run.completed",
            "created_at": "2026-07-21T00:00:00Z",
            "payload": {"status": "completed", "message_id": "42"},
        }
    )
    snapshots = [([], False), ([], False), ([terminal_event], True)]
    seen_after_seq: list[int] = []
    sleeps: list[float] = []

    async def events(**kwargs):
        seen_after_seq.append(kwargs["after_seq"])
        return snapshots.pop(0)

    async def sleep(delay):
        sleeps.append(delay)

    monotonic_values = iter((0.0, 10.0, 10.0))
    monkeypatch.setattr(queries, "owned_events", events)
    monkeypatch.setattr(completions.asyncio, "sleep", sleep)
    monkeypatch.setattr(completions, "monotonic", lambda: next(monotonic_values))

    response = await client.get("/api/v1/chat/runs/run-1/events")

    assert response.status_code == 200
    assert response.text.count(": keepalive") == 1
    assert sleeps == [0.1, 0.1]
    assert seen_after_seq == [0, 0, 0]
    assert "event: run.completed" in response.text


async def test_worker_flushes_small_delta_during_provider_pause_and_closes_iterator(monkeypatch):
    run = SimpleNamespace(
        id="run-1",
        model_name="model",
        project_id="project-1",
        user_id="user-1",
        conversation_id=None,
        user_message_id=None,
        assistant_message_id=None,
        capability_snapshot={},
        pricing_snapshot={
            "component_prices": {},
            "margin_multiplier": "1",
            "chat_credit_per_usd": "1",
        },
        execution_protocol_version=1,
        status="queued",
    )

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def begin(self):
            return self

    flushed = asyncio.Event()
    closed = asyncio.Event()
    journal: list[tuple[str, dict]] = []
    finished: list[dict] = []

    async def engine_stream(**_kwargs):
        try:
            yield {"type": "token", "text": "small"}
            await asyncio.wait_for(flushed.wait(), timeout=0.25)
            yield {"type": "usage", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        finally:
            closed.set()

    async def append(_run_id, event_type, payload, *, owner):
        journal.append((event_type, payload))
        if event_type == "part.delta":
            flushed.set()

    async def finish(_run_id, **kwargs):
        finished.append(kwargs)

    def usage_cost(*_args, **_kwargs):
        return SimpleNamespace(
            raw_cost=Decimal("0"),
            input_cost=Decimal("0"),
            output_cost=Decimal("0"),
            pricing_snapshot={},
        )

    async def no_cancel(_run_id):
        return False

    async def _return(value):
        return value

    monkeypatch.setattr(execution, "_factory", lambda: lambda: _Session())
    monkeypatch.setattr(execution, "claim_queued_run", lambda *_args, **_kwargs: _return(run))
    monkeypatch.setattr(
        execution,
        "_payload",
        lambda _run: {"input_messages": [{"role": "user", "content": "x"}], "features": {}},
    )
    monkeypatch.setattr(execution, "_validate_run_protocol_payload", lambda *_args: True)
    monkeypatch.setattr(
        execution.ps,
        "resolve_model_snapshot",
        lambda *_args, **_kwargs: _return({"model_name": "model", "provider_name": "provider"}),
    )
    monkeypatch.setattr(execution, "_managed_tool_configs", lambda *_args, **_kwargs: _return((None, None, None)))
    monkeypatch.setattr(execution.engine, "stream", engine_stream)
    monkeypatch.setattr(execution, "_append", append)
    monkeypatch.setattr(execution, "_finish", finish)
    monkeypatch.setattr(execution, "_set_stage", lambda *_args, **_kwargs: _return(None))
    monkeypatch.setattr(execution, "_cancel_requested", no_cancel)
    monkeypatch.setattr(execution, "_renew_lease", lambda *_args, **_kwargs: _return(True))
    monkeypatch.setattr(execution.credit, "precheck", lambda *_args, **_kwargs: _return(None))
    monkeypatch.setattr(execution.credit, "usage_cost_from_pricing_snapshot", usage_cost)
    monkeypatch.setattr(execution, "_append_temp_history", lambda *_args, **_kwargs: _return(None))

    assert await execution.execute_queued_run("run-1", owner="worker-1") is True
    assert [payload["delta"] for event_type, payload in journal if event_type == "part.delta"] == ["small"]
    assert finished[-1]["status"] == "completed"
    assert closed.is_set()


async def test_summary_compactor_fences_map_and_reduce_segments_and_records_usage(monkeypatch):
    run = SimpleNamespace(id="run-1", user_id="user-1", project_id="project-1", api_key_id=None)

    class _Result:
        def scalar_one(self):
            return run

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, *_args):
            return _Result()

    starts: list[dict] = []
    completes: list[dict] = []
    usage: list[dict] = []
    requests: list[dict] = []

    async def no_cancel(_run_id):
        return False

    async def resolve_snapshot(route):
        return {**route, "api_base": None, "api_key": "test-key", "provider_type": "openai"}

    async def complete_summary(**kwargs):
        requests.append(kwargs)
        return {
            "choices": [{"message": {"content": '{"summary":"compact","title":"Title"}'}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        }

    async def precheck(*_args, **_kwargs):
        return None

    async def start(**kwargs):
        starts.append(kwargs)
        return None

    async def complete(**kwargs):
        completes.append(kwargs)

    async def record(**kwargs):
        usage.append(kwargs)

    monkeypatch.setattr(execution, "_factory", lambda: lambda: _Session())
    monkeypatch.setattr(execution, "_cancel_requested", no_cancel)
    monkeypatch.setattr(execution.credit, "precheck", precheck)
    monkeypatch.setattr(execution.ps, "resolve_model_snapshot", resolve_snapshot)
    monkeypatch.setattr(execution.litellm_client, "acompletion", complete_summary)

    hooks = execution._DurableExecutionHooks(run_id="run-1", owner="worker-1")
    monkeypatch.setattr(hooks, "_start", start)
    monkeypatch.setattr(hooks, "_complete", complete)
    monkeypatch.setattr(hooks, "_record_summary_usage", record)
    route = {"model_name": "summary-model", "provider_name": "openai"}
    summary_messages = context_manager._summary_messages(["segment"], system_prompt=context_manager._SUMMARY_SYSTEM)

    for context in (
        {"phase": "map", "round": 0, "chunk_index": 0, "summary_messages": summary_messages},
        {"phase": "reduce", "round": 1, "chunk_index": 0, "summary_messages": summary_messages},
    ):
        result = await hooks._summary_compactor(
            ["segment"],
            context=context,
            route=route,
            run_context={"summary_output_reserve": 64},
        )
        assert result["summary"] == "compact"

    assert [entry["segment_id"] for entry in starts] == ["context:0:0:map", "context:1:0:reduce"]
    assert [entry["segment_id"] for entry in completes] == ["context:0:0:map", "context:1:0:reduce"]
    assert [entry["segment_id"] for entry in usage] == ["context:0:0:map", "context:1:0:reduce"]
    assert all(
        request["messages"][0] == {"role": "system", "content": context_manager._SUMMARY_SYSTEM} for request in requests
    )


async def test_tool_completion_registers_generated_artifact_ids(monkeypatch):
    captured: dict[str, object] = {}
    hooks = execution._DurableExecutionHooks(run_id="run-1", owner="worker-1")

    async def complete(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(hooks, "_complete", complete)

    await hooks.tool_completed(
        round_index=2,
        tool_index=3,
        tool_call_id="call-1",
        tool_name="python",
        result_payload={
            "content": "Created report.csv",
            "artifacts": [
                {
                    "asset_id": "asset-1",
                    "kind": "file",
                    "name": "report.csv",
                    "media_type": "text/csv",
                    "size_bytes": 42,
                }
            ],
        },
    )

    assert captured["output_asset_ids"] == ["asset-1"]
    event = captured["journal_completed"]
    assert isinstance(event, tuple)
    assert event[1]["content"][0]["asset_id"] == "asset-1"


async def test_summary_usage_keeps_the_originating_run_association(monkeypatch):
    run = SimpleNamespace(
        id="run-1",
        user_id="user-1",
        project_id="project-1",
        conversation_id="conversation-1",
        api_key_id=None,
        model_name="execution-model",
        pricing_snapshot={
            "summary_route": {
                "input_price_per_token": "0.001",
                "output_price_per_token": "0.002",
            },
            "margin_multiplier": "1",
        },
    )

    class _Result:
        def __init__(self, value):
            self.value = value

        def scalar_one(self):
            return self.value

        def scalar_one_or_none(self):
            return self.value

    class _Session:
        def __init__(self):
            self.results = iter((_Result(run), _Result(None)))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def begin(self):
            return self

        async def execute(self, *_args):
            return next(self.results)

    captured: list[dict] = []

    async def apply_usage(_session, **kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(execution, "_factory", lambda: lambda: _Session())
    monkeypatch.setattr(execution, "_require_owned_running_lease", lambda *_args: None)
    monkeypatch.setattr(
        execution.credit,
        "usage_cost_from_pricing_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(input_cost=Decimal("0.01"), output_cost=Decimal("0.02")),
    )
    monkeypatch.setattr(execution.credit, "apply_usage_in_transaction", apply_usage)

    await execution._DurableExecutionHooks(run_id="run-1", owner="worker-1")._record_summary_usage(
        segment_id="context:1:0:reduce",
        usage_payload={"prompt_tokens": 10, "completion_tokens": 5},
        route={"model_name": "summary-model", "provider_name": "openai"},
    )
    assert captured[0]["charge_wallet"] is False

    assert captured[0]["event_id"] == "context:run-1:context:1:0:reduce"
    assert captured[0]["run_id"] == "run-1"


async def test_parentless_virtual_run_skips_automatic_compaction(monkeypatch):
    run = SimpleNamespace(
        conversation_id=None,
        temp_thread_id=None,
        model_name="execution-model",
        capability_snapshot={"context_limit": 1_000},
    )

    class _Result:
        def scalar_one(self):
            return run

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, *_args):
            return _Result()

    state = SimpleNamespace(measurement="exact", input_budget=100, input_tokens=80, recommendation="required")
    monkeypatch.setattr(execution, "_factory", lambda: lambda: _Session())
    monkeypatch.setattr(execution, "_payload", lambda _run: {"max_tokens": 10})
    monkeypatch.setattr(execution.context_manager, "context_state", lambda *_args, **_kwargs: state)

    async def forbidden_compaction(*_args, **_kwargs):
        raise AssertionError("parentless virtual runs must not persist a checkpoint")

    monkeypatch.setattr(execution.context_manager, "prepare_model_messages", forbidden_compaction)
    messages = [{"role": "user", "content": "short request"}]
    result = await execution._DurableExecutionHooks(run_id="run-1", owner="worker-1").prepare_context(
        messages=messages,
        tool_schemas=[],
        round_index=0,
    )

    assert result is messages


async def test_automatic_compaction_warns_when_summary_budget_is_unavailable(monkeypatch):
    run = SimpleNamespace(
        conversation_id="conversation-1",
        temp_thread_id=None,
        model_name="execution-model",
        capability_snapshot={
            "context_limit": 1_000,
            "summary_route": {"model_name": "summary-model", "context_limit": None},
        },
    )

    class _Result:
        def scalar_one(self):
            return run

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, *_args):
            return _Result()

    appended: list[tuple[str, dict]] = []

    async def append(_run_id, event_type, payload, **_kwargs):
        appended.append((event_type, payload))

    state = SimpleNamespace(measurement="exact", input_budget=100, input_tokens=80, recommendation="required")
    monkeypatch.setattr(execution, "_factory", lambda: lambda: _Session())
    monkeypatch.setattr(execution, "_payload", lambda _run: {"max_tokens": 10})
    monkeypatch.setattr(execution.context_manager, "context_state", lambda *_args, **_kwargs: state)
    monkeypatch.setattr(execution, "_append", append)
    messages = [{"role": "user", "content": "short request"}]

    result = await execution._DurableExecutionHooks(run_id="run-1", owner="worker-1").prepare_context(
        messages=messages,
        tool_schemas=[],
        round_index=0,
    )

    assert result is messages
    assert appended == [
        (
            "run.warning",
            {
                "code": "context_budget_unavailable",
                "safe_message": "Summary context budget is unavailable; automatic compaction was skipped.",
            },
        )
    ]


async def test_manual_compaction_rejects_unavailable_summary_budget(monkeypatch):
    run = SimpleNamespace(
        conversation_id="conversation-1",
        temp_thread_id=None,
        model_name="execution-model",
        capability_snapshot={
            "context_limit": 1_000,
            "summary_route": {"model_name": "summary-model", "context_limit": None},
        },
    )

    class _Result:
        def scalar_one(self):
            return run

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, *_args):
            return _Result()

    state = SimpleNamespace(measurement="exact", input_budget=100, input_tokens=80, recommendation="required")
    monkeypatch.setattr(execution, "_factory", lambda: lambda: _Session())
    monkeypatch.setattr(execution, "_payload", lambda _run: {"max_tokens": 10})
    monkeypatch.setattr(execution.context_manager, "context_state", lambda *_args, **_kwargs: state)
    hooks = execution._DurableExecutionHooks(run_id="run-1", owner="worker-1")
    hooks.force_compaction = True

    with pytest.raises(context_manager.ContextLimitExceeded) as exc:
        await hooks.prepare_context(
            messages=[{"role": "user", "content": "short request"}],
            tool_schemas=[],
            round_index=0,
        )

    assert exc.value.code == "context_budget_unavailable"


async def test_compaction_worker_uses_persisted_large_tool_schema_snapshot(monkeypatch):
    large_schema = {
        "type": "function",
        "function": {
            "name": "large_schema",
            "description": "x" * 50_000,
            "parameters": {"type": "object"},
        },
    }
    run = SimpleNamespace(
        id="run-compact",
        model_name="execution-model",
        project_id="project-1",
        user_id="user-1",
        conversation_id="conversation-1",
        user_message_id=None,
        assistant_message_id=None,
        capability_snapshot={},
        pricing_snapshot={},
        run_kind="compaction",
    )

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def begin(self):
            return self

    captured: dict = {}

    class _Hooks:
        def __init__(self, **_kwargs):
            self.force_compaction = False

        async def prepare_context(self, **kwargs):
            captured.update(kwargs)

    async def claim(*_args, **_kwargs):
        return run

    async def no_cancel(*_args):
        return False

    async def finish(*_args, **kwargs):
        captured["finished"] = kwargs

    async def set_stage(*_args, **_kwargs):
        return None

    monkeypatch.setattr(execution, "_factory", lambda: lambda: _Session())
    monkeypatch.setattr(execution, "claim_queued_run", claim)
    monkeypatch.setattr(
        execution,
        "_payload",
        lambda _run: {"input_messages": [{"role": "user", "content": "old context"}], "tool_schemas": [large_schema]},
    )
    monkeypatch.setattr(execution, "_DurableExecutionHooks", _Hooks)
    monkeypatch.setattr(execution, "_set_stage", set_stage)
    monkeypatch.setattr(execution, "_cancel_requested", no_cancel)
    monkeypatch.setattr(execution, "_finish", finish)

    assert await execution.execute_queued_run("run-compact", owner="worker-1") is True
    assert captured["tool_schemas"] == [large_schema]
    assert captured["finished"]["status"] == "completed"


async def test_events_reject_mismatched_cursor_sources(client):
    response = await client.get(
        "/api/v1/chat/runs/run-1/events?after_seq=2",
        headers={"Last-Event-ID": "run-1:1"},
    )
    assert response.status_code == 400


async def test_events_return_gone_after_temporary_journal_purge(client, monkeypatch):
    async def events(**kwargs):
        raise durable_errors.DurableRunCursorExpired("temporary chat event journal has expired")

    monkeypatch.setattr(queries, "owned_events", events)
    response = await client.get("/api/v1/chat/runs/run-1/events", headers={"Last-Event-ID": "run-1:1"})

    assert response.status_code == 410


async def test_cancel_is_owner_scoped_and_idempotent(client, monkeypatch):
    seen = {}

    async def cancel(**kwargs):
        seen.update(kwargs)
        return {"run_id": "run-1", "status": "running"}

    monkeypatch.setattr(lifecycle, "request_cancelled", cancel)
    response = await client.post("/api/v1/chat/runs/run-1/cancel")

    assert response.status_code == 200
    assert seen["run_id"] == "run-1"
    assert seen["project_id"] == "test-project-123"
    assert seen["user_id"] == "test-user-123"


async def test_approval_decision_is_owner_scoped_and_validated(client, monkeypatch):
    seen = {}

    async def resolve(**kwargs):
        seen.update(kwargs)
        return {"run_id": "run-1", "call_id": "call-1", "decision": "approve", "status": "approved"}

    monkeypatch.setattr(interactions, "resolve_tool_approval", resolve)
    response = await client.post("/api/v1/chat/runs/run-1/approvals/call-1", json={"decision": "approve"})

    assert response.status_code == 200
    assert seen == {
        "run_id": "run-1",
        "call_id": "call-1",
        "decision": "approve",
        "project_id": "test-project-123",
        "user_id": "test-user-123",
    }

    invalid = await client.post("/api/v1/chat/runs/run-1/approvals/call-1", json={"decision": "later"})
    assert invalid.status_code == 422


async def test_interaction_response_is_owner_scoped_and_validated(client, monkeypatch):
    seen = {}

    async def resolve(**kwargs):
        seen.update(kwargs)
        return {"run_id": "run-1", "interaction_id": "interaction-1", "status": "answered"}

    monkeypatch.setattr(interactions, "resolve_run_interaction", resolve)
    response = await client.post(
        "/api/v1/chat/runs/run-1/interactions/interaction-1",
        json={"response": {"option_ids": ["yes"], "text": None}},
    )

    assert response.status_code == 200
    assert seen == {
        "run_id": "run-1",
        "interaction_id": "interaction-1",
        "response": {"option_ids": ["yes"], "text": None},
        "project_id": "test-project-123",
        "user_id": "test-user-123",
    }

    invalid = await client.post(
        "/api/v1/chat/runs/run-1/interactions/interaction-1",
        json={"response": {"option_ids": ["yes", "yes"], "extra": True}},
    )
    assert invalid.status_code == 422
