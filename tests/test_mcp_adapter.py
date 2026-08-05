from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_selected_snapshot_rejects_malformed_remote_payload(monkeypatch):
    from lumen.services import mcp_adapter

    async def bridge(*_args, **_kwargs):
        return {"snapshot": {"grant_id": "g"}}

    monkeypatch.setattr(mcp_adapter, "_bridge_post", bridge)
    with pytest.raises(mcp_adapter.McpLumenAuthorityError):
        await mcp_adapter.selected_lumen_snapshot(user_id="u", project_id="p")


@pytest.mark.asyncio
async def test_resolved_principal_requires_exact_frozen_snapshot(monkeypatch):
    from lumen.services import mcp_adapter

    snapshot = mcp_adapter.LumenSnapshot(
        grant_id="g", user_id="u", project_id="p", credential_epoch=1, selection_generation=2
    )

    async def bridge(*_args, **_kwargs):
        return {
            "snapshot": mcp_adapter.snapshot_payload(snapshot),
            "entries": [
                {
                    "name": "compute_list_servers",
                    "description": "List servers",
                    "input_schema": {"type": "object", "properties": {}},
                    "effect": "read",
                }
            ],
            "service_fingerprint": "a" * 64,
        }

    monkeypatch.setattr(mcp_adapter, "_bridge_post", bridge)
    principal = await mcp_adapter.resolve_lumen_principal(snapshot)

    assert principal.snapshot == snapshot
    assert principal.service_fingerprint == "a" * 64
    assert [entry.name for entry in mcp_adapter.enabled_entries(principal)] == ["compute_list_servers"]


@pytest.mark.asyncio
async def test_mutation_claim_returns_remote_execution_as_replay(monkeypatch):
    from lumen.services import mcp_adapter

    snapshot = mcp_adapter.LumenSnapshot(
        grant_id="g", user_id="u", project_id="p", credential_epoch=1, selection_generation=2
    )
    principal = mcp_adapter.Principal("u", "p", snapshot, (), "a" * 64)
    entry = mcp_adapter.RegistryEntry("compute_create_server", "Create", "external_mutation", {})
    observed = {}

    async def bridge(path, payload):
        observed["path"] = path
        observed["payload"] = payload
        return {"status": "ok", "server_id": "server"}

    monkeypatch.setattr(mcp_adapter, "_bridge_post", bridge)
    claim = await mcp_adapter.claim_mutation(
        principal, entry=entry, arguments={"name": "test"}, idempotency_key="lumen-key", source="lumen"
    )

    assert claim.state == "replay"
    assert claim.result == {"status": "ok", "server_id": "server"}
    assert observed == {
        "path": "/execute",
        "payload": {
            "snapshot": mcp_adapter.snapshot_payload(snapshot),
            "name": "compute_create_server",
            "arguments": {"name": "test"},
            "idempotency_key": "lumen-key",
        },
    }
