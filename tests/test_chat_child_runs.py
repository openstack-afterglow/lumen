"""Public delegation argument and result boundaries (no database required)."""

from decimal import Decimal

import pytest

from lumen.services.durable_runs.children import ChildFailure, child_result_payload, parse_delegation_call


@pytest.mark.parametrize(
    "change",
    [
        {"agent_id": True},
        {"task": "   "},
        {"credit_budget": "NaN"},
        {"credit_budget": "0"},
        {"sandbox_seconds": False},
        {"sandbox_seconds": 0},
        {"access": "admin"},
    ],
)
def test_invalid_delegation_never_becomes_a_child_request(change):
    arguments = {"agent_id": 7, "task": "Inspect the result", "access": "read", "credit_budget": "1.25", "sandbox_seconds": 30}
    with pytest.raises(ChildFailure) as error:
        parse_delegation_call("call-1", {**arguments, **change})
    assert error.value.code == "invalid_tool_arguments"


def test_child_result_exposes_bounded_summary_and_owned_asset_references_only():
    result = child_result_payload(
        status="failed",
        error_code="sandbox_unavailable",
        summary="x" * 9000,
        artifacts=[
            {"asset_id": "asset-1", "name": "report.txt", "mime_type": "text/plain", "size_bytes": 12, "private_token": "secret"},
            {"name": "unpersisted.txt", "size_bytes": 2},
        ],
    )
    assert result == {
        "status": "failed",
        "error_code": "sandbox_unavailable",
        "summary": "x" * 8192,
        "artifacts": [{"type": "file", "asset_id": "asset-1", "name": "report.txt", "mime_type": "text/plain", "size_bytes": 12}],
    }
    parsed = parse_delegation_call("call-1", {"agent_id": 7, "task": "Inspect", "credit_budget": "1.25", "sandbox_seconds": 30})
    assert parsed.credit_budget == Decimal("1.25")
    assert parsed.access == "read"
