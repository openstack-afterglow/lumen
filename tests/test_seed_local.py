from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

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
    assert "models:read" in _LOCAL_KEY_SCOPES

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
        "user_id": "local-console-user",
        "project_id": "local-console-project",
        "api_key_id": 1,
        "scopes": tuple(_LOCAL_KEY_SCOPES),
    }
    assert is_seed_key_current(current) is True
    assert is_seed_key_current({**current, "user_id": "foreign-user"}) is False
    assert is_seed_key_current({**current, "project_id": "foreign-project"}) is False
    assert is_seed_key_current({**current, "scopes": ("models:read",)}) is False
    assert is_seed_key_current(None) is False


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


@pytest.mark.asyncio
async def test_seed_reconciles_provider_and_model_when_fields_differ(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:

    seed_file = tmp_path / "api-key"
    conn_file = tmp_path / "connection.json"
    monkeypatch.setenv("DATABASE_URL", "mysql+aiomysql://lumen:lumen@127.0.0.1:3306/lumen")
    monkeypatch.setenv("LUMEN_LOCAL_SEED_PATH", str(seed_file))
    monkeypatch.setenv("LUMEN_LOCAL_CONNECTION_PATH", str(conn_file))
    monkeypatch.setenv("LUMEN_LOCAL_PROVIDER_TYPE", "openai")
    monkeypatch.setenv("LUMEN_LOCAL_PROVIDER_BASE_URL", "http://new-provider-base:8000")
    monkeypatch.setenv("LUMEN_LOCAL_INPUT_PRICE_PER_MILLION", "5")
    monkeypatch.setenv("LUMEN_LOCAL_OUTPUT_PRICE_PER_MILLION", "15")

    fake_provider = {
        "id": 1,
        "name": "local-openai",
        "provider_type": "openai",
        "api_base": "http://old-provider-base:8000",
        "api_key_env": "LUMEN_LOCAL_PROVIDER_API_KEY",
    }
    fake_model = {
        "id": 10,
        "provider_id": 1,
        "model_name": "gpt-4.1-mini",
        "input_price_per_million": "1.00000000",
        "output_price_per_million": "3.00000000",
    }

    mock_list_providers = AsyncMock(return_value=[fake_provider])
    mock_update_provider = AsyncMock(return_value={**fake_provider, "api_base": "http://new-provider-base:8000"})
    mock_list_models = AsyncMock(return_value=[fake_model])
    mock_update_model = AsyncMock(
        return_value={**fake_model, "input_price_per_million": "5", "output_price_per_million": "15"}
    )
    mock_verify_key = AsyncMock(return_value=None)
    mock_create_key = AsyncMock(return_value={"key": "sk-afgl-new-key-123"})
    mock_close_db = AsyncMock()

    monkeypatch.setattr("lumen.services.providers.repository.list_providers", mock_list_providers)
    monkeypatch.setattr("lumen.services.providers.repository.update_provider", mock_update_provider)
    monkeypatch.setattr("lumen.services.providers.repository.list_models", mock_list_models)
    monkeypatch.setattr("lumen.services.providers.repository.update_model", mock_update_model)
    monkeypatch.setattr("lumen.services.api_key_store.verify_key", mock_verify_key)
    monkeypatch.setattr("lumen.services.api_key_store.create_key", mock_create_key)
    monkeypatch.setattr("lumen.scripts.seed_local.init_db", lambda _url: None)
    monkeypatch.setattr("lumen.scripts.seed_local.close_db", mock_close_db)

    from lumen.scripts.seed_local import seed

    await seed()

    mock_update_provider.assert_called_once_with(1, {"api_base": "http://new-provider-base:8000"})
    mock_update_model.assert_called_once_with(10, {"input_price_per_million": "5", "output_price_per_million": "15"})
    assert conn_file.exists()
    manifest = json.loads(conn_file.read_text())
    assert manifest["api_key"] == "sk-afgl-new-key-123"


@pytest.mark.asyncio
async def test_seed_rotates_legacy_local_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seed_file = tmp_path / "api-key"
    seed_file.write_text("sk-afgl-legacy\n")
    connection_file = tmp_path / "connection.json"
    monkeypatch.setenv("DATABASE_URL", "mysql+aiomysql://lumen:lumen@127.0.0.1:3306/lumen")
    monkeypatch.setenv("LUMEN_LOCAL_SEED_PATH", str(seed_file))
    monkeypatch.setenv("LUMEN_LOCAL_CONNECTION_PATH", str(connection_file))
    monkeypatch.setenv("LUMEN_LOCAL_PROVIDER_API_KEY", "provider-key")

    provider = {
        "id": 1,
        "name": "local-openai",
        "provider_type": "openai",
        "api_base": None,
        "api_key_env": "LUMEN_LOCAL_PROVIDER_API_KEY",
        "has_api_key": True,
    }
    model = {
        "id": 10,
        "provider_id": 1,
        "model_name": "gpt-4.1-mini",
        "input_price_per_million": "1",
        "output_price_per_million": "3",
    }
    legacy_key = {
        "user_id": "local-console-user",
        "project_id": "local-console-project",
        "api_key_id": 7,
        "scopes": tuple(scope for scope in _LOCAL_KEY_SCOPES if scope != "compat:completions:write"),
    }
    revoke_key = AsyncMock()
    create_key = AsyncMock(return_value={"key": "sk-afgl-current"})
    monkeypatch.setattr("lumen.services.providers.repository.list_providers", AsyncMock(return_value=[provider]))
    monkeypatch.setattr("lumen.services.providers.repository.list_models", AsyncMock(return_value=[model]))
    monkeypatch.setattr("lumen.services.api_key_store.verify_key", AsyncMock(return_value=legacy_key))
    monkeypatch.setattr("lumen.services.api_key_store.revoke_key", revoke_key)
    monkeypatch.setattr("lumen.services.api_key_store.create_key", create_key)
    monkeypatch.setattr("lumen.scripts.seed_local.init_db", lambda _url: None)
    monkeypatch.setattr("lumen.scripts.seed_local.close_db", AsyncMock())

    from lumen.scripts.seed_local import seed

    await seed()

    revoke_key.assert_awaited_once_with(7, "local-console-user", "local-console-project")
    assert "compat:completions:write" in create_key.await_args.args[3]
    assert seed_file.read_text() == "sk-afgl-current\n"
    assert json.loads(connection_file.read_text())["api_key"] == "sk-afgl-current"
