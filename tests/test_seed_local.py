from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from lumen.scripts.seed_local import (
    _LOCAL_KEY_SCOPES,
    is_scope_satisfied,
    is_seed_key_current,
    normalize_base_url,
    write_connection_manifest,
)
from lumen.scripts.show_connection import show_connection


def test_normalize_base_url_valid() -> None:
    assert normalize_base_url("http://127.0.0.1:8012") == "http://127.0.0.1:8012/v1"
    assert normalize_base_url("http://127.0.0.1:8012/") == "http://127.0.0.1:8012/v1"
    assert normalize_base_url("http://127.0.0.1:8012/v1") == "http://127.0.0.1:8012/v1"
    assert normalize_base_url("http://127.0.0.1:8012/v1/") == "http://127.0.0.1:8012/v1"
    assert normalize_base_url("http://127.0.0.1:8012/v1/v1") == "http://127.0.0.1:8012/v1"
    assert normalize_base_url("https://my-domain.org:9000/api") == "https://my-domain.org:9000/api/v1"
    assert normalize_base_url("https://my-domain.org:9000/api/v1/") == "https://my-domain.org:9000/api/v1"


def test_normalize_base_url_invalid() -> None:
    invalid_urls = [
        "",
        "   ",
        "ftp://127.0.0.1:8012",
        "not-a-url",
        "http://",
        "://8012",
        "http://user:secret@127.0.0.1:8012",
        "http://127.0.0.1:8012/v1?token=secret",
        "http://127.0.0.1:8012/v1#fragment",
        "http://127.0.0.1:invalid",
        "http://127.0.0.1:0",
        "http://not valid:8012",
    ]
    for invalid_url in invalid_urls:
        with pytest.raises(ValueError):
            normalize_base_url(invalid_url)


def test_required_scope_rotation_predicate() -> None:
    assert "compat:completions:write" in _LOCAL_KEY_SCOPES
    assert "native:conversations:read" in _LOCAL_KEY_SCOPES
    assert "native:conversations:write" in _LOCAL_KEY_SCOPES
    assert "models:read" in _LOCAL_KEY_SCOPES
    assert {"compat:images:write", "compat:audio:write", "compat:realtime:write"} <= set(_LOCAL_KEY_SCOPES)
    assert {"native:images:write", "native:audio:write", "native:realtime:write",
            "native:assets:read", "native:assets:write"} <= set(_LOCAL_KEY_SCOPES)
    assert {"native:batches:read", "native:batches:write", "compat:batches:read", "compat:batches:write",
            "compat:files:read", "compat:files:write"} <= set(_LOCAL_KEY_SCOPES)

    # Complete current scopes -> satisfied
    assert is_scope_satisfied(_LOCAL_KEY_SCOPES, _LOCAL_KEY_SCOPES) is True

    # Legacy scopes missing compat:completions:write -> dissatisfied (requires rotation)
    legacy_scopes = [
        "models:read",
        "native:runs:read",
        "native:runs:write",
        "native:extensions:read",
        "native:tools:execute",
        "native:memory:read",
        "native:memory:write",
        "usage:read",
    ]
    assert is_scope_satisfied(legacy_scopes, _LOCAL_KEY_SCOPES) is False

    # Extra scopes -> satisfied
    extra_scopes = _LOCAL_KEY_SCOPES + ["admin:all"]
    assert is_scope_satisfied(extra_scopes, _LOCAL_KEY_SCOPES) is True


def test_seed_key_current_requires_local_owner_project_and_scopes() -> None:
    current = {
        "user_id": "keystone-owner-id",
        "project_id": "keystone-project-id",
        "api_key_id": 1,
        "scopes": tuple(_LOCAL_KEY_SCOPES),
    }
    assert is_seed_key_current(current, "keystone-owner-id", "keystone-project-id") is True
    assert is_seed_key_current({**current, "user_id": "foreign-user"}, "keystone-owner-id", "keystone-project-id") is False
    assert is_seed_key_current({**current, "project_id": "foreign-project"}, "keystone-owner-id", "keystone-project-id") is False
    assert is_seed_key_current({**current, "scopes": ("models:read",)}, "keystone-owner-id", "keystone-project-id") is False
    assert is_seed_key_current(None, "keystone-owner-id", "keystone-project-id") is False


def test_write_connection_manifest_schema_and_permissions(tmp_path: Path) -> None:
    manifest_path = tmp_path / "connection.json"
    manifest_data = {
        "schema_version": 1,
        "base_url": "http://127.0.0.1:8012/v1",
        "container_base_url": "http://lumen-api:8012/v1",
        "api_key": "sk-afgl-test-key-12345",
        "model": "gpt-4.1-mini",
        "provider_api_key_configured": True,
    }

    write_connection_manifest(manifest_path, manifest_data)

    assert manifest_path.exists()
    assert (manifest_path.stat().st_mode & 0o777) == 0o600

    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert loaded == manifest_data


def test_cli_missing_manifest_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing_path = tmp_path / "non_existent.json"
    monkeypatch.setenv("LUMEN_LOCAL_CONNECTION_PATH", str(missing_path))

    with pytest.raises(SystemExit) as exc_info:
        show_connection()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Connection manifest not found" in captured.err
    assert captured.out == ""


def test_cli_corrupt_manifest_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    corrupt_path = tmp_path / "connection.json"
    corrupt_path.write_text("{bad json content ... api_key: secret", encoding="utf-8")
    monkeypatch.setenv("LUMEN_LOCAL_CONNECTION_PATH", str(corrupt_path))

    with pytest.raises(SystemExit) as exc_info:
        show_connection()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Failed to read or parse connection manifest" in captured.err
    assert "secret" not in captured.err
    assert captured.out == ""


def test_cli_invalid_schema_or_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest_path = tmp_path / "connection.json"
    monkeypatch.setenv("LUMEN_LOCAL_CONNECTION_PATH", str(manifest_path))

    # Wrong schema version
    bad_manifest = {
        "schema_version": 2,
        "base_url": "http://127.0.0.1:8012/v1",
        "container_base_url": "http://lumen-api:8012/v1",
        "api_key": "sk-afgl-test-key",
        "model": "gpt-4.1-mini",
        "provider_api_key_configured": True,
    }
    manifest_path.write_text(json.dumps(bad_manifest), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        show_connection()
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "invalid schema_version" in captured.err

    # Invalid field type (provider_api_key_configured as string)
    bad_type_manifest = {
        "schema_version": 1,
        "base_url": "http://127.0.0.1:8012/v1",
        "container_base_url": "http://lumen-api:8012/v1",
        "api_key": "sk-afgl-test-key",
        "model": "gpt-4.1-mini",
        "provider_api_key_configured": "true",
    }
    manifest_path.write_text(json.dumps(bad_type_manifest), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        show_connection()
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "invalid provider_api_key_configured" in captured.err


def test_cli_valid_manifest_prints_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest_path = tmp_path / "connection.json"
    monkeypatch.setenv("LUMEN_LOCAL_CONNECTION_PATH", str(manifest_path))

    valid_data = {
        "schema_version": 1,
        "base_url": "http://127.0.0.1:8012/v1",
        "container_base_url": "http://lumen-api:8012/v1",
        "api_key": "sk-afgl-valid-key-9999",
        "model": "gpt-4.1-mini",
        "provider_api_key_configured": False,
    }
    write_connection_manifest(manifest_path, valid_data)

    show_connection()

    captured = capsys.readouterr()
    assert captured.err == ""
    parsed_output = json.loads(captured.out)
    assert parsed_output == valid_data


def test_cli_rejects_extra_fields_and_insecure_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest_path = tmp_path / "connection.json"
    monkeypatch.setenv("LUMEN_LOCAL_CONNECTION_PATH", str(manifest_path))
    manifest = {
        "schema_version": 1,
        "base_url": "http://127.0.0.1:8012/v1",
        "container_base_url": "http://lumen-api:8012/v1",
        "api_key": "sk-afgl-valid-key",
        "model": "gpt-4.1-mini",
        "provider_api_key_configured": True,
        "unexpected": "must-not-be-printed",
    }
    write_connection_manifest(manifest_path, manifest)

    with pytest.raises(SystemExit):
        show_connection()
    assert "unexpected fields" in capsys.readouterr().err

    manifest.pop("unexpected")
    write_connection_manifest(manifest_path, manifest)
    manifest_path.chmod(0o644)
    with pytest.raises(SystemExit):
        show_connection()
    assert "insecure file permissions" in capsys.readouterr().err


def _setup_seed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, providers: dict, rows: list[dict]):
    from lumen.scripts import seed_local

    monkeypatch.setenv("DATABASE_URL", "mysql+aiomysql://lumen:lumen@localhost/lumen")
    from lumen.service_authority import SERVICE_CAPABILITIES

    monkeypatch.setenv("LUMEN_LOCAL_OWNER_USER_ID", "keystone-owner-id")
    monkeypatch.setenv("LUMEN_LOCAL_OWNER_PROJECT_ID", "keystone-project-id")
    monkeypatch.setattr(seed_local, "resolve_project_authority", AsyncMock(return_value={
        "roles": ["member", *SERVICE_CAPABILITIES], "is_system_admin": False,
    }))
    monkeypatch.setenv("LUMEN_LOCAL_SEED_PATH", str(tmp_path / "api-key"))
    monkeypatch.setenv("LUMEN_LOCAL_CONNECTION_PATH", str(tmp_path / "connection.json"))
    for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "LUMEN_LOCAL_PROVIDER_BASE_URL",
                 "LUMEN_LOCAL_PROVIDER_NAME", "LUMEN_LOCAL_MODEL", "LUMEN_LOCAL_CONTEXT_LIMIT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LUMEN_LOCAL_PROVIDER_TYPE", "openai")
    monkeypatch.setattr(seed_local, "init_db", lambda _url: None)
    monkeypatch.setattr(seed_local, "close_db", AsyncMock())
    bootstrap = AsyncMock(return_value=providers)
    monkeypatch.setattr(seed_local, "seed_environment_providers", bootstrap)
    monkeypatch.setattr(seed_local.repository, "list_providers", AsyncMock(return_value=list(providers.values())))
    monkeypatch.setattr(seed_local.repository, "list_models", AsyncMock(return_value=rows))
    create_provider = AsyncMock(return_value={"id": 90, "name": "local-openai", "has_api_key": False})
    create_model = AsyncMock()
    update_provider = AsyncMock()
    monkeypatch.setattr(seed_local.repository, "create_provider", create_provider)
    monkeypatch.setattr(seed_local.repository, "create_model", create_model)
    monkeypatch.setattr(seed_local.repository, "update_provider", update_provider)
    monkeypatch.setattr(seed_local.api_key_store, "verify_key", AsyncMock(return_value=None))
    monkeypatch.setattr(seed_local.api_key_store, "create_key", AsyncMock(return_value={"key": "sk-afgl-new"}))
    return seed_local, bootstrap, create_provider, create_model, update_provider


@pytest.mark.parametrize("missing", ["LUMEN_LOCAL_OWNER_USER_ID", "LUMEN_LOCAL_OWNER_PROJECT_ID"])
async def test_seed_requires_explicit_real_owner_before_provider_changes(monkeypatch, tmp_path, missing):
    local, bootstrap, create_provider, create_model, _ = _setup_seed(monkeypatch, tmp_path, {}, [])
    monkeypatch.delenv(missing)
    with pytest.raises(RuntimeError, match=missing):
        await local.seed()
    local.resolve_project_authority.assert_not_awaited()
    bootstrap.assert_not_awaited()
    create_provider.assert_not_awaited()
    create_model.assert_not_awaited()
    local.api_key_store.create_key.assert_not_awaited()
    assert not (tmp_path / "api-key").exists()


@pytest.mark.parametrize("authority", [
    {"roles": ["member"], "is_system_admin": False},
    {"roles": ["member", "lumen-keys_editor", "lumen-chat_user"], "is_system_admin": False},
    HTTPException(status_code=403, detail="membership removed"),
    HTTPException(status_code=503, detail="Keystone unavailable"),
])
async def test_seed_rejects_unusable_owner_before_provider_changes(monkeypatch, tmp_path, authority):
    local, bootstrap, create_provider, create_model, _ = _setup_seed(monkeypatch, tmp_path, {}, [])
    if isinstance(authority, Exception):
        local.resolve_project_authority.side_effect = authority
    else:
        local.resolve_project_authority.return_value = authority
    with pytest.raises(RuntimeError, match="current Keystone"):
        await local.seed()
    local.resolve_project_authority.assert_awaited_once_with("keystone-owner-id", "keystone-project-id")
    bootstrap.assert_not_awaited()
    create_provider.assert_not_awaited()
    create_model.assert_not_awaited()
    local.api_key_store.create_key.assert_not_awaited()
    assert not (tmp_path / "connection.json").exists()


async def test_seed_rotates_owned_outdated_key_with_current_delete_scope(monkeypatch, tmp_path):
    local, _, _, _, _ = _setup_seed(monkeypatch, tmp_path, {}, [])
    (tmp_path / "api-key").write_text("sk-afgl-old\n")
    local.api_key_store.verify_key.return_value = {
        "user_id": "keystone-owner-id", "project_id": "keystone-project-id",
        "api_key_id": 7, "scopes": ("models:read",),
    }
    revoke = AsyncMock()
    monkeypatch.setattr(local.api_key_store, "revoke_key", revoke)
    await local.seed()
    revoke.assert_awaited_once_with(7, "keystone-owner-id", "keystone-project-id")
    local.api_key_store.create_key.assert_awaited_once()
    assert (tmp_path / "api-key").read_text() == "sk-afgl-new\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("keys,expected_model,expected_ids", [
    ({"OPENAI_API_KEY": "openai-secret"}, "gpt-4.1-mini", [1]),
    ({"GEMINI_API_KEY": "gemini-secret"}, "gemini/gemini-2.5-flash", [2]),
    ({"OPENAI_API_KEY": "openai-secret", "GEMINI_API_KEY": "gemini-secret"},
     "gpt-4.1-mini", [1, 2]),
])
async def test_seed_uses_direct_providers_and_creates_only_missing_text_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, keys: dict, expected_model: str, expected_ids: list[int]
) -> None:
    providers = {
        "openai": {"id": 1, "name": "openai", "provider_type": "openai", "has_api_key": True},
        "gemini": {"id": 2, "name": "gemini", "provider_type": "gemini", "has_api_key": True},
    }
    local, bootstrap, create_provider, create_model, update_provider = _setup_seed(
        monkeypatch, tmp_path, providers, []
    )
    for key, value in keys.items():
        monkeypatch.setenv(key, value)
    await local.seed()
    bootstrap.assert_awaited_once_with()
    create_provider.assert_not_awaited()
    update_provider.assert_not_awaited()
    assert [call.kwargs["provider_id"] for call in create_model.await_args_list] == expected_ids
    assert [call.kwargs["model_name"] for call in create_model.await_args_list] == (
        [expected_model, "gemini/gemini-2.5-flash"] if len(expected_ids) == 2 else [expected_model]
    )
    for call in create_model.await_args_list:
        if call.kwargs["provider_id"] == 2:
            assert call.kwargs.get("input_price_per_million") is None
            assert call.kwargs.get("output_price_per_million") is None
    manifest = json.loads((tmp_path / "connection.json").read_text())
    assert manifest["model"] == expected_model
    assert manifest["provider_api_key_configured"] is True


@pytest.mark.asyncio
async def test_seed_preserves_admin_model_prices_and_provider_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider = {"id": 1, "name": "openai", "provider_type": "openai", "has_api_key": True,
                "api_base": "https://admin.example/v1", "api_key_source": "database"}
    model = {"provider_id": 1, "model_name": "gpt-4.1-mini", "input_price_per_million": "42",
             "output_price_per_million": "99", "capabilities": {"context_limit": 8192}, "is_active": False}
    local, _, _, create_model, update_provider = _setup_seed(monkeypatch, tmp_path, {"openai": provider}, [model])
    monkeypatch.setenv("OPENAI_API_KEY", "configured")
    monkeypatch.setenv("LUMEN_LOCAL_INPUT_PRICE_PER_MILLION", "5")
    await local.seed()
    create_model.assert_not_awaited()
    update_provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_custom_fake_route_and_rotates_missing_media_scopes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider = {"id": 3, "name": "fake-openai", "provider_type": "openai", "has_api_key": True,
                "api_base": "http://fake-provider:8080/v1", "api_key_env": "OPENAI_API_KEY"}
    local, _, create_provider, create_model, _ = _setup_seed(monkeypatch, tmp_path, {"openai": {
        "id": 1, "name": "openai", "provider_type": "openai", "has_api_key": True
    }}, [])
    local.repository.list_providers.return_value.append(provider)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-provider-key")
    monkeypatch.setenv("LUMEN_LOCAL_PROVIDER_NAME", "fake-openai")
    monkeypatch.setenv("LUMEN_LOCAL_PROVIDER_BASE_URL", "http://fake-provider:8080/v1")
    monkeypatch.setenv("LUMEN_LOCAL_MODEL", "fake-gpt-4")
    old_key = tmp_path / "api-key"
    old_key.write_text("sk-afgl-legacy\n")
    local.api_key_store.verify_key.return_value = {
        "user_id": "keystone-owner-id", "project_id": "keystone-project-id", "api_key_id": 7,
        "scopes": tuple(scope for scope in _LOCAL_KEY_SCOPES if scope != "native:assets:read"),
    }
    revoke = AsyncMock()
    monkeypatch.setattr(local.api_key_store, "revoke_key", revoke)
    await local.seed()
    create_provider.assert_not_awaited()
    assert create_model.await_args.kwargs["provider_id"] == 3
    assert create_model.await_args.kwargs["model_name"] == "fake-gpt-4"
    revoke.assert_awaited_once_with(7, "keystone-owner-id", "keystone-project-id")
    assert "native:assets:read" in local.api_key_store.create_key.await_args.args[3]
    assert local.api_key_store.create_key.await_args.args[:2] == ("keystone-owner-id", "keystone-project-id")
    assert old_key.read_text() == "sk-afgl-new\n"
    assert json.loads((tmp_path / "connection.json").read_text())["model"] == "fake-gpt-4"


@pytest.mark.asyncio
async def test_seed_migrates_only_unmodified_legacy_binding_without_duplicating_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    primary = {"id": 1, "name": "openai", "provider_type": "openai", "has_api_key": True}
    legacy = {"id": 7, "name": "local-openai", "provider_type": "openai", "api_base": None,
              "api_key_env": "LUMEN_LOCAL_PROVIDER_API_KEY", "has_api_key": False}
    local, _, _, create_model, update_provider = _setup_seed(
        monkeypatch, tmp_path, {"openai": primary}, [{"provider_id": 7, "model_name": "gpt-4.1-mini",
                                             "input_price_per_million": "83"}]
    )
    local.repository.list_providers.return_value.append(legacy)
    update_provider.return_value = {**legacy, "api_key_env": "OPENAI_API_KEY", "has_api_key": True}
    monkeypatch.setenv("OPENAI_API_KEY", "configured")
    await local.seed()
    update_provider.assert_awaited_once_with(7, {"api_key_env": "OPENAI_API_KEY"})
    create_model.assert_not_awaited()
    assert json.loads((tmp_path / "connection.json").read_text())["provider_api_key_configured"] is True
