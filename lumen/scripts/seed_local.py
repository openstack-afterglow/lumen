"""Seed a runnable standalone provider, model, and scoped local API connection."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Collection
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

from fastapi import HTTPException

from lumen.auth import ensure_scopes, resolve_project_authority
from lumen.db import close_db, init_db
from lumen.scripts.seed_providers import seed_environment_providers
from lumen.services import api_key_store
from lumen.services.providers import repository

_LOCAL_KEY_SCOPES = [
    "models:read",
    "compat:completions:write",
    "compat:images:write",
    "compat:audio:write",
    "compat:realtime:write",
    "compat:batches:read",
    "compat:batches:write",
    "compat:files:read",
    "compat:files:write",
    "native:conversations:read",
    "native:conversations:write",
    "native:runs:read",
    "native:runs:write",
    "native:images:write",
    "native:audio:write",
    "native:realtime:write",
    "native:assets:read",
    "native:assets:write",
    "native:batches:read",
    "native:batches:write",
    "native:extensions:read",
    "native:tools:execute",
    "native:memory:read",
    "native:memory:write",
    "usage:read",
]


def normalize_base_url(value: str) -> str:
    candidate = (value or "").strip()
    if not candidate or any(char.isspace() for char in candidate):
        raise ValueError("base URL must be a non-empty HTTP(S) URL")
    parsed = urlsplit(candidate)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("base URL has an invalid port") from exc
    if port == 0:
        raise ValueError("base URL has an invalid port")
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base URL must be an absolute HTTP(S) URL without credentials, query, or fragment")

    path = parsed.path.rstrip("/")
    while path.endswith("/v1"):
        path = path[:-3].rstrip("/")
    normalized_path = f"{path}/v1"
    return parsed._replace(path=normalized_path, query="", fragment="").geturl()


def is_scope_satisfied(verified_scopes: Collection[str], required_scopes: Collection[str]) -> bool:
    return set(required_scopes).issubset(verified_scopes)


def is_seed_key_current(verified: dict | None, user_id: str, project_id: str) -> bool:
    return bool(
        verified
        and verified.get("user_id") == user_id
        and verified.get("project_id") == project_id
        and is_scope_satisfied(verified.get("scopes", ()), _LOCAL_KEY_SCOPES)
    )


def _write_private_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.fchmod(temporary.fileno(), 0o600)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def write_connection_manifest(path: Path, manifest: dict) -> None:
    _write_private_text(path, json.dumps(manifest, indent=2) + "\n")




def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be set for the local Lumen stack")
    return value


async def seed() -> None:
    database_url = _required("DATABASE_URL")
    user_id = _required("LUMEN_LOCAL_OWNER_USER_ID")
    project_id = _required("LUMEN_LOCAL_OWNER_PROJECT_ID")
    try:
        authority = await resolve_project_authority(user_id, project_id)
        ensure_scopes(authority, "native:keys:write", *_LOCAL_KEY_SCOPES)
    except HTTPException as exc:
        raise RuntimeError(
            f"Local API key owner {user_id!r} in project {project_id!r} must have current Keystone "
            f"membership, keys-editor and all requested service capabilities: {exc.detail}"
        ) from exc
    provider_name = os.environ.get("LUMEN_LOCAL_PROVIDER_NAME", "local-openai").strip()
    provider_type = os.environ.get("LUMEN_LOCAL_PROVIDER_TYPE", "openai").strip()
    requested_model = os.environ.get("LUMEN_LOCAL_MODEL", "").strip()
    seed_path = Path(os.environ.get("LUMEN_LOCAL_SEED_PATH", "/seed/api-key"))
    conn_path_env = os.environ.get("LUMEN_LOCAL_CONNECTION_PATH", "").strip()
    connection_path = Path(conn_path_env) if conn_path_env else seed_path.parent / "connection.json"

    public_base_url_env = os.environ.get("LUMEN_LOCAL_PUBLIC_BASE_URL", "").strip()
    if public_base_url_env:
        base_url = normalize_base_url(public_base_url_env)
    else:
        api_port = os.environ.get("LUMEN_API_PORT", "8012").strip() or "8012"
        base_url = normalize_base_url(f"http://127.0.0.1:{api_port}/v1")

    container_base_url_env = (
        os.environ.get("LUMEN_LOCAL_CONTAINER_BASE_URL", "http://lumen-api:8012/v1").strip()
        or "http://lumen-api:8012/v1"
    )
    container_base_url = normalize_base_url(container_base_url_env)

    init_db(database_url)
    try:
        environment_providers = await seed_environment_providers()
        providers = await repository.list_providers()
        models = await repository.list_models(active_only=False)
        custom_base = os.environ.get("LUMEN_LOCAL_PROVIDER_BASE_URL", "").strip() or None
        openai_enabled = bool(os.environ.get("OPENAI_API_KEY", "").strip())
        gemini_enabled = bool(os.environ.get("GEMINI_API_KEY", "").strip())
        custom_provider = bool(custom_base or provider_name != "local-openai" or provider_type != "openai")

        # Keep the isolated system stack on its fake upstream instead of sending
        # its fake model name to the direct OpenAI endpoint.
        if custom_provider or not (openai_enabled or gemini_enabled):
            selected_type = provider_type
            model_name = requested_model or "gpt-4.1-mini"
            target_name = provider_name
        else:
            selected_type = "openai" if openai_enabled else "gemini"
            model_name = requested_model or (
                "gpt-4.1-mini" if openai_enabled else "gemini/gemini-2.5-flash"
            )
            target_name = selected_type

        if selected_type == "gemini" and not model_name.startswith("gemini/"):
            model_name = f"gemini/{model_name}"

        env_name = "GEMINI_API_KEY" if selected_type == "gemini" else "OPENAI_API_KEY"
        provider = next((item for item in providers if item["name"] == target_name), None)
        if not custom_provider and (openai_enabled or gemini_enabled):
            provider = environment_providers.get(target_name) or provider

        # A previous local seed may already own this public model name. Reuse
        # the old row rather than introducing an ambiguous duplicate in a
        # persistent MariaDB volume. Never change its model or price settings.
        if target_name == "openai":
            legacy = next((item for item in providers if item["name"] == "local-openai"), None)
            if (legacy and legacy.get("provider_type") == "openai" and legacy.get("api_base") is None
                    and legacy.get("auth_mode", "api_key") == "api_key"
                    and any(item["provider_id"] == legacy["id"] and item["model_name"] == model_name
                            for item in models)):
                if legacy.get("api_key_env") == "LUMEN_LOCAL_PROVIDER_API_KEY" and not legacy.get("api_key_source"):
                    legacy = await repository.update_provider(legacy["id"], {"api_key_env": "OPENAI_API_KEY"})
                if legacy.get("api_key_env") == "OPENAI_API_KEY" or legacy.get("api_key_source"):
                    provider = legacy

        if provider is None:
            provider = await repository.create_provider(
                name=target_name,
                provider_type=selected_type,
                api_base=custom_base,
                api_key_env=env_name,
            )
        elif (custom_provider and provider.get("provider_type") == selected_type
              and provider.get("auth_mode", "api_key") == "api_key"
              and provider.get("api_base") == custom_base
              and not provider.get("api_key_env") and not provider.get("api_key_source")):
            provider = await repository.update_provider(provider["id"], {"api_key_env": env_name})

        raw_context_limit = os.environ.get("LUMEN_LOCAL_CONTEXT_LIMIT", "").strip()
        try:
            local_context_limit = int(raw_context_limit) if raw_context_limit else None
        except ValueError as exc:
            raise RuntimeError("LUMEN_LOCAL_CONTEXT_LIMIT must be a positive integer") from exc
        if local_context_limit is not None and local_context_limit <= 0:
            raise RuntimeError("LUMEN_LOCAL_CONTEXT_LIMIT must be a positive integer")
        capabilities = {"context_limit": local_context_limit} if local_context_limit is not None else None

        if not any(item["provider_id"] == provider["id"] and item["model_name"] == model_name
                   for item in models):
            # Gemini direct API models use the exact bundled catalog. Missing
            # prices remain unpriced rather than inheriting local OpenAI rates.
            prices = (None, None) if selected_type == "gemini" and not custom_base else (
                os.environ.get("LUMEN_LOCAL_INPUT_PRICE_PER_MILLION", "1").strip(),
                os.environ.get("LUMEN_LOCAL_OUTPUT_PRICE_PER_MILLION", "3").strip(),
            )
            await repository.create_model(
                provider_id=provider["id"], model_name=model_name,
                input_price_per_million=prices[0], output_price_per_million=prices[1],
                capabilities=capabilities,
            )

        # Both credentials should make a text model usable, even when the
        # connection manifest points to just one preferred provider.
        if openai_enabled and gemini_enabled and not custom_provider:
            gemini = environment_providers.get("gemini")
            gemini_model = "gemini/gemini-2.5-flash"
            if gemini and not any(item["provider_id"] == gemini["id"] and item["model_name"] == gemini_model
                                  for item in models):
                await repository.create_model(provider_id=gemini["id"], model_name=gemini_model)

        raw_key = seed_path.read_text().strip() if seed_path.exists() else ""
        verified = await api_key_store.verify_key(raw_key) if raw_key else None

        if verified is not None and not is_seed_key_current(verified, user_id, project_id):
            if verified.get("user_id") == user_id and verified.get("project_id") == project_id:
                ensure_scopes(authority, "native:keys:delete")
                await api_key_store.revoke_key(
                    verified["api_key_id"],
                    user_id,
                    project_id,
                )
            verified = None
        if verified is None:
            issued = await api_key_store.create_key(
                user_id,
                project_id,
                "local-console",
                _LOCAL_KEY_SCOPES,
                None,
            )
            raw_key = issued["key"]
        _write_private_text(seed_path, f"{raw_key}\n")

        provider_key_configured = bool(provider.get("has_api_key"))
        manifest = {
            "schema_version": 1,
            "base_url": base_url,
            "container_base_url": container_base_url,
            "api_key": raw_key,
            "model": model_name,
            "provider_api_key_configured": provider_key_configured,
        }
        write_connection_manifest(connection_path, manifest)

        provider_key_status = "configured" if provider_key_configured else "not configured"
        print(f"Lumen provider API key: {provider_key_status}", flush=True)
        print(f"Lumen model: {model_name}", flush=True)
        print(f"Lumen base URL: {base_url}", flush=True)
        print(f"Lumen connection manifest written to: {connection_path}", flush=True)
    finally:
        await close_db()


def main() -> None:
    asyncio.run(seed())


if __name__ == "__main__":
    main()
