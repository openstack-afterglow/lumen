from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from lumen.models.chat_contracts import (
    ChatFeatureOptions,
    CompletionRequest,
    UsageComponent,
    validate_chat_parts,
    validate_chat_run_event,
    validate_user_input_parts,
)


def test_default_feature_options_preserve_manual_memory_without_tools():
    assert ChatFeatureOptions().model_dump(by_alias=True) == {
        "web_search": {
            "enabled": False,
            "context_size": "medium",
            "allowed_domains": [],
            "blocked_domains": [],
            "approximate_location": None,
            "max_uses": 1,
            "provider_id": None,
        },
        "web_fetch": {"enabled": False, "allowed_domains": [], "blocked_domains": [], "max_uses": 1},
        "advisor": {"enabled": False, "max_uses": 1, "model_id": None},
        "memory": True,
        "response_format": {"kind": "text", "name": None, "version": None, "schema": None},
        "tool_policy": {
            "mode": "agent_default",
            "approval_mode": "required_for_mutations",
            "enabled_tool_ids": None,
            "enabled_mcp_ids": None,
            "workspace_write_mode": "ask",
        },
        "output_modalities": ["text"],
        "image_output": None,
        "audio_output": None,
        "video_output": None,
    }


def test_completion_request_accepts_only_iana_client_timezones():
    request = CompletionRequest(
        parts=[{"type": "text", "text": "hello"}],
        model_id="model",
        client_timezone="Asia/Seoul",
    )
    assert request.client_timezone == "Asia/Seoul"
    with pytest.raises(ValidationError, match="client_timezone"):
        CompletionRequest(
            parts=[{"type": "text", "text": "hello"}],
            model_id="model",
            client_timezone="not-a-timezone",
        )


def test_json_schema_format_allows_bounded_subset_only():
    features = ChatFeatureOptions(
        response_format={
            "kind": "json_schema",
            "name": "answer",
            "version": "1",
            "schema": {
                "type": "object",
                "properties": {"answer": {"type": "string", "minLength": 1}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        }
    )
    assert features.response_format.schema_["properties"]["answer"]["type"] == "string"
    with pytest.raises(ValidationError, match="unsupported keywords"):
        ChatFeatureOptions(
            response_format={
                "kind": "json_schema",
                "name": "unsafe",
                "version": "1",
                "schema": {"$ref": "https://example.test/schema"},
            }
        )
    with pytest.raises(ValidationError, match="required is invalid"):
        ChatFeatureOptions(
            response_format={
                "kind": "json_schema",
                "name": "invalid",
                "version": "1",
                "schema": {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["missing"]},
            }
        )


def test_tool_policy_rejects_non_numeric_or_duplicate_extension_ids():
    assert ChatFeatureOptions(tool_policy={"enabled_tool_ids": ["1", "20"]}).tool_policy.enabled_tool_ids == [1, 20]
    with pytest.raises(ValidationError, match="enabled_tool_ids"):
        ChatFeatureOptions(tool_policy={"enabled_tool_ids": ["1", "1"]})
    with pytest.raises(ValidationError, match="enabled_tool_ids"):
        ChatFeatureOptions(tool_policy={"enabled_tool_ids": ["mcp-1"]})


def test_enabled_search_and_advisor_require_user_selected_routes():
    with pytest.raises(ValidationError, match="web_search requires provider_id"):
        ChatFeatureOptions(web_search={"enabled": True})
    with pytest.raises(ValidationError, match="advisor requires model_id"):
        ChatFeatureOptions(advisor={"enabled": True})
    options = ChatFeatureOptions(
        web_search={"enabled": True, "provider_id": 7},
        advisor={"enabled": True, "model_id": 9},
    )
    assert options.web_search.provider_id == 7
    assert options.advisor.model_id == 9


def test_web_domains_are_idna_normalized_and_conflicts_fail_closed():
    options = ChatFeatureOptions(
        web_search={
            "enabled": True,
            "provider_id": 7,
            "allowed_domains": ["BÜCHER.example"],
            "blocked_domains": ["cdn.example"],
            "approximate_location": {"country": "KR", "city": "Seoul"},
        },
        web_fetch={"allowed_domains": ["docs.example"]},
    )
    assert options.web_search.allowed_domains == ["xn--bcher-kva.example"]
    assert options.web_search.approximate_location == {"country": "KR", "city": "Seoul"}
    with pytest.raises(ValidationError, match="overlap"):
        ChatFeatureOptions(web_search={"allowed_domains": ["example.com"], "blocked_domains": ["api.example.com"]})
    with pytest.raises(ValidationError, match="hostname"):
        ChatFeatureOptions(web_fetch={"allowed_domains": ["https://example.com"]})
    with pytest.raises(ValidationError, match="unsupported"):
        ChatFeatureOptions(web_search={"approximate_location": {"postal_code": "12345"}})


def test_user_input_accepts_owned_asset_ids_only():
    assert validate_user_input_parts([{"type": "text", "text": "hello"}, {"type": "image", "asset_id": "asset-1"}])
    with pytest.raises(ValidationError):
        validate_user_input_parts([{"type": "audio", "asset_id": "asset-1", "transcript": "client supplied"}])
    with pytest.raises(ValidationError):
        validate_user_input_parts([{"type": "image", "url": "https://example.test/a.png"}])


@pytest.mark.parametrize("delta", ["\n", "foo "])
def test_streamed_text_whitespace_round_trips_without_normalization(delta):
    event = validate_chat_run_event(
        {
            "event_id": "run-1:1",
            "run_id": "run-1",
            "seq": 1,
            "type": "part.delta",
            "created_at": datetime.now(UTC).isoformat(),
            "payload": {
                "message_id": "message-1",
                "part_index": 0,
                "part_type": "text",
                "delta": delta,
            },
        }
    )
    completed = validate_chat_run_event(
        {
            "event_id": "run-1:2",
            "run_id": "run-1",
            "seq": 2,
            "type": "part.completed",
            "created_at": datetime.now(UTC).isoformat(),
            "payload": {
                "message_id": "message-1",
                "part_index": 0,
                "part": {"type": "text", "text": delta},
            },
        }
    )
    parts = validate_chat_parts(
        [
            {"type": "text", "text": delta},
            {"type": "reasoning", "text": delta, "visibility": "user"},
        ]
    )

    assert event.payload.delta == delta
    assert completed.payload.part.model_dump()["text"] == delta
    assert [part.model_dump()["text"] for part in parts] == [delta, delta]


def test_parts_reject_hidden_reasoning_and_deep_tool_results():
    with pytest.raises(ValidationError):
        validate_chat_parts([{"type": "reasoning", "text": "hidden", "visibility": "provider"}])

    part: dict = {"type": "text", "text": "ok"}
    for _ in range(5):
        part = {
            "type": "tool_result",
            "call_id": "call",
            "name": "tool",
            "content": [part],
            "is_error": False,
        }
    with pytest.raises(ValueError, match="nesting"):
        validate_chat_parts([part])


def test_usage_decimal_contract_allows_only_provider_adjustment_credit():
    base = {
        "segment_id": "seg-1",
        "kind": "input_tokens",
        "quantity": "1.25",
        "unit": "token",
        "unit_price_usd": "0.0001",
        "cost_usd": "0.000125",
        "source": "executor",
        "model_name": "model",
    }
    assert UsageComponent(**base).quantity == "1.25"
    with pytest.raises(ValidationError):
        UsageComponent(**{**base, "cost_usd": "-0.1"})
    assert UsageComponent(**{**base, "kind": "provider_adjustment", "cost_usd": "-0.1"}).cost_usd == "-0.1"


def test_run_event_requires_monotonic_opaque_cursor_and_typed_payload():
    event = validate_chat_run_event(
        {
            "event_id": "run-1:1",
            "run_id": "run-1",
            "seq": 1,
            "type": "run.started",
            "created_at": datetime.now(UTC).isoformat(),
            "payload": {
                "conversation_id": None,
                "temp_thread_id": None,
                "model_name": "model",
                "effective_features": {},
            },
        }
    )
    assert event.type == "run.started"
    with pytest.raises(ValidationError, match="event_id"):
        validate_chat_run_event(
            {
                "event_id": "run-1:2",
                "run_id": "run-1",
                "seq": 1,
                "type": "run.started",
                "created_at": datetime.now(UTC).isoformat(),
                "payload": {
                    "conversation_id": None,
                    "temp_thread_id": None,
                    "model_name": "model",
                    "effective_features": {},
                },
            }
        )


def test_run_stage_event_requires_named_tool_only_during_execution():
    base = {
        "event_id": "run-1:2",
        "run_id": "run-1",
        "seq": 2,
        "type": "run.stage.changed",
        "created_at": datetime.now(UTC).isoformat(),
    }
    event = validate_chat_run_event({**base, "payload": {"stage": "tool_execution", "tool_name": "web_search"}})
    assert event.payload.stage == "tool_execution"
    with pytest.raises(ValidationError, match="tool_execution"):
        validate_chat_run_event({**base, "payload": {"stage": "tool_execution", "tool_name": None}})
    with pytest.raises(ValidationError, match="only tool_execution"):
        validate_chat_run_event({**base, "payload": {"stage": "queued", "tool_name": "web_search"}})


def test_tool_approval_resolved_event_has_decision_provenance():
    event = validate_chat_run_event(
        {
            "event_id": "run-1:3",
            "run_id": "run-1",
            "seq": 3,
            "type": "tool.approval_resolved",
            "created_at": datetime.now(UTC).isoformat(),
            "payload": {
                "call_id": "call-1",
                "decision": "deny",
                "decided_by_user_id": None,
                "decided_at": datetime.now(UTC).isoformat(),
            },
        }
    )
    assert event.payload.decided_by_user_id is None
    with pytest.raises(ValidationError):
        validate_chat_run_event(
            {
                "event_id": "run-1:3",
                "run_id": "run-1",
                "seq": 3,
                "type": "tool.approval_resolved",
                "created_at": datetime.now(UTC).isoformat(),
                "payload": {"call_id": "call-1", "decision": "deny"},
            }
        )


def test_tool_approval_required_event_exposes_only_redacted_frozen_dispatch_details():
    base = {
        "event_id": "run-1:4",
        "run_id": "run-1",
        "seq": 4,
        "type": "tool.approval_required",
        "created_at": datetime.now(UTC).isoformat(),
        "payload": {
            "call_id": "call-1",
            "name": "workspace.write_file",
            "source": "workspace",
            "effect": "workspace_write",
            "destination": "workspace:ws-1",
            "redacted_arguments": {"path": "src/app.py", "content": "[REDACTED]"},
            "preview": [{"type": "text", "text": "Modify src/app.py"}],
            "expected_state_revision": 7,
            "writer_fence": 3,
            "expires_at": datetime.now(UTC).isoformat(),
        },
    }
    event = validate_chat_run_event(base)
    assert event.payload.redacted_arguments["content"] == "[REDACTED]"
    with pytest.raises(ValidationError):
        validate_chat_run_event({**base, "payload": {**base["payload"], "arguments": {"content": "secret"}}})


def test_interaction_resolved_event_has_bounded_typed_payload():
    event = validate_chat_run_event(
        {
            "event_id": "run-1:3",
            "run_id": "run-1",
            "seq": 3,
            "type": "interaction.resolved",
            "created_at": datetime.now(UTC).isoformat(),
            "payload": {
                "interaction_id": "75dcc8d9-dc8d-460b-a6ca-6a5fdd1e10d6",
                "status": "answered",
                "response": {"option_ids": ["yes"], "text": None},
            },
        }
    )
    assert event.payload.status == "answered"
    with pytest.raises(ValidationError):
        validate_chat_run_event(
            {
                "event_id": "run-1:3",
                "run_id": "run-1",
                "seq": 3,
                "type": "interaction.resolved",
                "created_at": datetime.now(UTC).isoformat(),
                "payload": {
                    "interaction_id": "75dcc8d9-dc8d-460b-a6ca-6a5fdd1e10d6",
                    "status": "answered",
                    "response": {"option_ids": ["yes"], "text": "x" * 4_001, "extra": "rejected"},
                },
            }
        )
    with pytest.raises(ValidationError):
        validate_chat_run_event(
            {
                "event_id": "run-1:3",
                "run_id": "run-1",
                "seq": 3,
                "type": "interaction.resolved",
                "created_at": datetime.now(UTC).isoformat(),
                "payload": {
                    "interaction_id": "75dcc8d9-dc8d-460b-a6ca-6a5fdd1e10d6",
                    "status": "timeout",
                    "response": {"option_ids": ["yes"], "text": None},
                },
            }
        )
    with pytest.raises(ValidationError):
        validate_chat_run_event(
            {
                "event_id": "run-1:3",
                "run_id": "run-1",
                "seq": 3,
                "type": "interaction.resolved",
                "created_at": datetime.now(UTC).isoformat(),
                "payload": {"interaction_id": "i", "status": "invalid"},
            }
        )


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("run.completed", {"status": "completed", "message_id": "42"}),
        (
            "run.failed",
            {
                "status": "failed",
                "message_id": None,
                "error_code": "provider_error",
                "safe_message": "The provider failed.",
            },
        ),
        (
            "run.canceled",
            {"status": "canceled", "message_id": "42", "error_code": "canceled_by_user", "safe_message": "Canceled."},
        ),
    ],
)
def test_terminal_events_have_type_specific_payloads(event_type, payload):
    event = validate_chat_run_event(
        {
            "event_id": "run-1:2",
            "run_id": "run-1",
            "seq": 2,
            "type": event_type,
            "created_at": datetime.now(UTC).isoformat(),
            "payload": payload,
        }
    )

    assert event.type == event_type


def test_text_execution_boundary_never_silently_drops_asset_parts():
    from lumen.models.chat_contracts import UnsupportedInputPartError, text_from_user_input_parts

    assert (
        text_from_user_input_parts([{"type": "text", "text": "first"}, {"type": "text", "text": "second"}])
        == "first\nsecond"
    )
    with pytest.raises(UnsupportedInputPartError, match="image"):
        text_from_user_input_parts([{"type": "image", "asset_id": "asset-1"}])
