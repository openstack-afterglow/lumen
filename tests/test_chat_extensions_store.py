from types import SimpleNamespace

import pytest

from lumen.services import extensions_store as es


def _mcp(server_id: int) -> dict:
    return {
        "id": server_id,
        "name": f"mcp-{server_id}",
        "transport": "http",
        "url": "https://mcp.example",
        "config_version": 1,
    }


async def test_frozen_mcp_selection_requires_matching_active_credential_version(monkeypatch):
    current = _mcp(7)
    snapshot = {
        "tools": [],
        "mcp": [
            {
                "id": 7,
                "config_fingerprint": es.selection_fingerprint(current),
                "credential_version": 3,
            }
        ],
    }

    async def list_visible(kind, **_kwargs):
        return [current] if kind == "mcp" else []

    monkeypatch.setattr(es, "list_for_user", list_visible)
    monkeypatch.setattr(es, "mcp_credential_versions", lambda *_args, **_kwargs: _return({7: 3}))

    tools, mcp, warnings = await es.validated_frozen_selection(snapshot, user_id="u1", project_id="p1")

    assert tools == ()
    assert mcp == (7,)
    assert warnings == []


@pytest.mark.parametrize("current_version", [4, None])
async def test_frozen_mcp_selection_blocks_rotated_or_revoked_credential(monkeypatch, current_version):
    current = _mcp(7)
    snapshot = {
        "tools": [],
        "mcp": [
            {
                "id": 7,
                "config_fingerprint": es.selection_fingerprint(current),
                "credential_version": 3,
            }
        ],
    }

    async def list_visible(kind, **_kwargs):
        return [current] if kind == "mcp" else []

    monkeypatch.setattr(es, "list_for_user", list_visible)
    monkeypatch.setattr(es, "mcp_credential_versions", lambda *_args, **_kwargs: _return({7: current_version}))

    _, mcp, warnings = await es.validated_frozen_selection(snapshot, user_id="u1", project_id="p1")

    assert mcp == ()
    assert warnings == ["selected MCP credential changed or was revoked"]


def test_frozen_selection_fingerprints_only_keeps_validated_selected_entries():
    snapshot = {
        "tools": [{"id": 2, "config_fingerprint": "tool-fingerprint"}, {"id": 3, "config_fingerprint": "unused"}],
        "mcp": [{"id": 7, "config_fingerprint": "mcp-fingerprint"}],
    }

    assert es.frozen_selection_fingerprints(snapshot, (2,), (7,)) == (
        ("tool", 2, "tool-fingerprint"),
        ("mcp", 7, "mcp-fingerprint"),
    )


def test_revealing_corrupt_mcp_headers_fails_closed(monkeypatch):
    server = SimpleNamespace(
        id=7,
        scope="global",
        encrypted_headers="invalid-ciphertext",
        headers=None,
        name="mcp",
        transport="http",
        url="https://mcp.example",
        is_active=True,
        tool_effect_overrides=None,
        config_version=1,
    )
    monkeypatch.setattr(es, "decrypt_llm_provider_key", lambda _value: "not-json")

    with pytest.raises(es.ExtensionSecretUnavailable):
        es._reveal_mcp(server)


async def _return(value):
    return value
