from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_selected_snapshot_rejects_malformed_remote_payload(monkeypatch):
    from lumen.services import mcp_adapter

    async def bridge(*_args, **_kwargs):
        return {"snapshot": {"grant_id": "g"}}

    monkeypatch.setattr(mcp_adapter, "_bridge_post", bridge)
    with pytest.raises(mcp_adapter.McpLumenAuthorityError):
        await mcp_adapter.selected_lumen_snapshots(user_id="u", project_id="p")


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


def _view(grant_id: str, fingerprint: str, entry_name: str) -> dict:
    return {
        "snapshot": {
            "grant_id": grant_id,
            "user_id": "u",
            "project_id": "p",
            "credential_epoch": 1,
            "selection_generation": 2,
        },
        "entries": [
            {
                "name": entry_name,
                "description": "d",
                "input_schema": {"type": "object", "properties": {}},
                "effect": "read",
            }
        ],
        "service_fingerprint": fingerprint,
    }


@pytest.mark.asyncio
async def test_selected_snapshots_lists_every_enabled_grant(monkeypatch):
    from lumen.services import mcp_adapter

    async def bridge(*_args, **_kwargs):
        return {"snapshots": [_view("g1", "a" * 64, "x"), _view("g2", "b" * 64, "y")]}

    monkeypatch.setattr(mcp_adapter, "_bridge_post", bridge)
    snapshots = await mcp_adapter.selected_lumen_snapshots(user_id="u", project_id="p")
    assert [snapshot.grant_id for snapshot in snapshots] == ["g1", "g2"]


@pytest.mark.asyncio
async def test_legacy_single_view_bridge_response_still_yields_one_grant(monkeypatch):
    from lumen.services import mcp_adapter

    legacy = _view("g1", "a" * 64, "x")

    async def bridge(*_args, **_kwargs):
        return {"snapshot": legacy["snapshot"], "entries": legacy["entries"], "service_fingerprint": "a" * 64}

    monkeypatch.setattr(mcp_adapter, "_bridge_post", bridge)
    snapshots = await mcp_adapter.selected_lumen_snapshots(user_id="u", project_id="p")
    assert [snapshot.grant_id for snapshot in snapshots] == ["g1"]


@pytest.mark.asyncio
async def test_resolve_uses_the_entries_of_the_matching_grant_only(monkeypatch):
    from lumen.services import mcp_adapter

    second = mcp_adapter.LumenSnapshot(
        grant_id="g2", user_id="u", project_id="p", credential_epoch=1, selection_generation=2
    )

    async def bridge(*_args, **_kwargs):
        return {"snapshots": [_view("g1", "a" * 64, "x"), _view("g2", "b" * 64, "y")]}

    monkeypatch.setattr(mcp_adapter, "_bridge_post", bridge)
    principal = await mcp_adapter.resolve_lumen_principal(second)
    assert [entry.name for entry in principal.entries] == ["y"]
    assert principal.service_fingerprint == "b" * 64


def test_frozen_snapshots_accepts_legacy_dict_and_list_forms():
    from lumen.services import mcp_adapter

    one = mcp_adapter.snapshot_payload(
        mcp_adapter.LumenSnapshot(
            grant_id="g1", user_id="u", project_id="p", credential_epoch=1, selection_generation=2
        )
    )
    assert mcp_adapter.frozen_snapshots(None) == []
    assert [s.grant_id for s in mcp_adapter.frozen_snapshots(one)] == ["g1"]
    assert [s.grant_id for s in mcp_adapter.frozen_snapshots([one])] == ["g1"]
    assert mcp_adapter.snapshot_payloads([]) is None
