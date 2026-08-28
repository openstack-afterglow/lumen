"""Seed a runnable standalone provider, model, and scoped local API connection."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Collection
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

from lumen.db import close_db, init_db
from lumen.services import api_key_store
from lumen.services.providers import repository

_LOCAL_USER_ID = "local-console-user"
_LOCAL_PROJECT_ID = "local-console-project"
_LOCAL_KEY_SCOPES = [
    "models:read",
    "compat:completions:write",
    "native:runs:read",
    "native:runs:write",
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


def is_seed_key_current(verified: dict | None) -> bool:
    return bool(
        verified
        and verified.get("user_id") == _LOCAL_USER_ID
        and verified.get("project_id") == _LOCAL_PROJECT_ID
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


def _decimal_differs(current: object, target: str) -> bool:
    try:
        return Decimal(str(current)) != Decimal(target)
    except (InvalidOperation, ValueError):
        return str(current) != target


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be set for the local Lumen stack")
    return value


async def seed() -> None:
    database_url = _required("DATABASE_URL")
    provider_name = os.environ.get("LUMEN_LOCAL_PROVIDER_NAME", "local-openai").strip()
    provider_type = os.environ.get("LUMEN_LOCAL_PROVIDER_TYPE", "openai").strip()
    model_name = os.environ.get("LUMEN_LOCAL_MODEL", "gpt-4.1-mini").strip()
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
        providers = await repository.list_providers()
        provider = next((item for item in providers if item["name"] == provider_name), None)
        target_provider_type = provider_type
        target_api_base = os.environ.get("LUMEN_LOCAL_PROVIDER_BASE_URL", "").strip() or None
        target_api_key_env = "LUMEN_LOCAL_PROVIDER_API_KEY"

        if provider is None:
            provider = await repository.create_provider(
                name=provider_name,
                provider_type=target_provider_type,
                api_base=target_api_base,
                api_key_env=target_api_key_env,
            )
        else:
            provider_patch: dict = {}
            if provider.get("provider_type") != target_provider_type:
                provider_patch["provider_type"] = target_provider_type
            if provider.get("api_base") != target_api_base:
                provider_patch["api_base"] = target_api_base
            if provider.get("api_key_env") != target_api_key_env:
                provider_patch["api_key_env"] = target_api_key_env

            if provider_patch:
                provider = await repository.update_provider(provider["id"], provider_patch)

        target_input_price = os.environ.get("LUMEN_LOCAL_INPUT_PRICE_PER_MILLION", "1").strip()
        target_output_price = os.environ.get("LUMEN_LOCAL_OUTPUT_PRICE_PER_MILLION", "3").strip()

        models = await repository.list_models(active_only=False)
        model = next(
            (item for item in models if item["provider_id"] == provider["id"] and item["model_name"] == model_name),
            None,
        )

        if model is None:
            await repository.create_model(
                provider_id=provider["id"],
                model_name=model_name,
                input_price_per_million=target_input_price,
                output_price_per_million=target_output_price,
            )
        else:
            in_differs = _decimal_differs(model.get("input_price_per_million"), target_input_price)
            out_differs = _decimal_differs(model.get("output_price_per_million"), target_output_price)

            if in_differs or out_differs:
                await repository.update_model(
                    model["id"],
                    {
                        "input_price_per_million": target_input_price,
                        "output_price_per_million": target_output_price,
                    },
                )

        raw_key = seed_path.read_text().strip() if seed_path.exists() else ""
        verified = await api_key_store.verify_key(raw_key) if raw_key else None

        if verified is not None and not is_seed_key_current(verified):
            if verified.get("user_id") == _LOCAL_USER_ID and verified.get("project_id") == _LOCAL_PROJECT_ID:
                await api_key_store.revoke_key(
                    verified["api_key_id"],
                    _LOCAL_USER_ID,
                    _LOCAL_PROJECT_ID,
                )
            verified = None
        if verified is None:
            issued = await api_key_store.create_key(
                _LOCAL_USER_ID,
                _LOCAL_PROJECT_ID,
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
