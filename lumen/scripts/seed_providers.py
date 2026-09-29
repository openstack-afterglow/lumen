"""Idempotent, credential-safe provider bootstrap from the process environment.

``seed_environment_providers()`` (DB already initialized) creates the ``openai``
and ``gemini`` API-key providers when OPENAI_API_KEY / GEMINI_API_KEY are
present. Only the variable *name* is persisted; the key is read at request time.
Optional LUMEN_BOOTSTRAP_MODELS_JSON lists explicitly priced models:
``{provider, model_name, model_kind: 'text', input_price_per_million,
output_price_per_million}`` or ``{provider, model_name, model_kind, media_pricing}``.
Existing providers and models are never overwritten.
"""

from __future__ import annotations

import asyncio
import json
import os

from lumen.db import close_db, init_db
from lumen.services.litellm_client import direct_provider_route
from lumen.services.providers import repository
from lumen.services.providers.audio_transport import validate_audio_request
from lumen.services.providers.credentials import api_model_name, canonical_subscription_model_name
from lumen.services.providers.errors import ProviderValidationError
from lumen.services.providers.image_transport import validate_image_request
from lumen.services.providers.pricing import MODEL_KINDS, _validate_price_pair, validate_media_pricing
from lumen.services.providers.realtime_transport import validate_realtime_request

ENVIRONMENT_PROVIDERS = {"openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}
MODELS_ENV = "LUMEN_BOOTSTRAP_MODELS_JSON"
_TEXT_FIELDS = {"provider", "model_name", "model_kind", "input_price_per_million", "output_price_per_million"}
_MEDIA_FIELDS = {"provider", "model_name", "model_kind", "media_pricing"}


def _validation_route(provider: dict, model_name: str, kind: str, pricing: dict) -> dict:
    return {
        "provider_type": provider["provider_type"],
        "api_base": provider.get("api_base"),
        "provider_auth": None,
        "api_key": "present",  # Readiness sentinel; the credential is never read here.
        "api_model_name": api_model_name(model_name, provider["provider_type"]),
        "model_kind": kind,
        "media_pricing": pricing,
    }


def _validate_media_route(provider: dict, model_name: str, kind: str, pricing: dict) -> None:
    if (provider.get("auth_mode", "api_key") != "api_key"
            or not direct_provider_route(provider["provider_type"], provider.get("api_base"))):
        raise ProviderValidationError("media model provider is not a supported direct API-key route")
    route = _validation_route(provider, model_name, kind, pricing)
    if kind == "image":
        variants = pricing.get("image_variants")
        if not isinstance(variants, dict) or not variants:
            raise ProviderValidationError("image model needs exact size:quality variant prices")
        for variant in variants:
            size, quality = variant.rsplit(":", 1)
            validate_image_request(route, size=size, quality=quality, n=1, edit=False)
    elif kind == "tts":
        validate_audio_request(route, kind="tts", format="wav" if provider["provider_type"] == "gemini" else "mp3")
    elif kind == "stt":
        validate_audio_request(route, kind="stt")
    else:
        validate_realtime_request(route)


def _parse_models(raw: str, providers: dict[str, dict]) -> list[dict]:
    """Validate every entry before any write; unsupported or unpriced entries fail."""
    if not raw.strip():
        return []
    try:
        entries = json.loads(raw)
    except ValueError:
        raise ProviderValidationError(f"{MODELS_ENV} is not valid JSON") from None
    if not isinstance(entries, list):
        raise ProviderValidationError(f"{MODELS_ENV} must be a JSON list")
    planned: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(entries):
        label = f"{MODELS_ENV}[{index}]"
        if not isinstance(entry, dict):
            raise ProviderValidationError(f"{label} must be an object")
        kind = entry.get("model_kind")
        if not isinstance(kind, str) or kind not in MODEL_KINDS:
            raise ProviderValidationError(f"{label} has an unsupported model_kind")
        allowed = _TEXT_FIELDS if kind == "text" else _MEDIA_FIELDS
        if set(entry) != allowed:
            raise ProviderValidationError(f"{label} must contain exactly: {', '.join(sorted(allowed))}")
        provider_name, model_name = entry["provider"], entry["model_name"]
        if provider_name not in ENVIRONMENT_PROVIDERS:
            raise ProviderValidationError(f"{label} provider must be one of: {', '.join(ENVIRONMENT_PROVIDERS)}")
        if not isinstance(model_name, str) or not model_name.strip() or model_name != model_name.strip():
            raise ProviderValidationError(f"{label} has an invalid model_name")
        canonical_subscription_model_name(model_name, "api_key")
        public_name = api_model_name(model_name, provider_name)
        if (provider_name, public_name) in seen:
            raise ProviderValidationError(f"{label} duplicates another model entry")
        seen.add((provider_name, public_name))
        provider = providers.get(provider_name) or {"provider_type": provider_name, "api_base": None}
        if provider["provider_type"] != provider_name:
            raise ProviderValidationError(f"{label} provider {provider_name!r} has an incompatible provider_type")
        if kind == "text":
            prices = (entry["input_price_per_million"], entry["output_price_per_million"])
            if None in prices:
                raise ProviderValidationError(f"{label} needs explicit input and output prices")
            _validate_price_pair(*prices)
            planned.append({"provider": provider_name, "model_name": model_name, "model_kind": kind,
                            "input_price_per_million": prices[0], "output_price_per_million": prices[1]})
        else:
            pricing = validate_media_pricing(kind, entry["media_pricing"])
            if not pricing:
                raise ProviderValidationError(f"{label} needs explicit media_pricing")
            _validate_media_route(provider, model_name, kind, pricing)
            planned.append({"provider": provider_name, "model_name": model_name, "model_kind": kind,
                            "media_pricing": pricing})
    return planned


async def _bootstrap() -> tuple[dict[str, dict], int]:
    existing = {row["name"]: row for row in await repository.list_providers()}
    planned = _parse_models(os.environ.get(MODELS_ENV, ""), existing)

    for name, env_name in ENVIRONMENT_PROVIDERS.items():
        if not os.environ.get(env_name, "").strip():
            continue
        row = existing.get(name)
        if row is None:
            try:
                existing[name] = await repository.create_provider(name=name, provider_type=name, api_key_env=env_name)
            except ProviderValidationError:
                # A concurrent bootstrap/admin created the name first; its row wins.
                raced = {item["name"]: item for item in await repository.list_providers()}.get(name)
                if raced is None:
                    raise
                existing[name] = raced
        elif (row["provider_type"] == name and row.get("auth_mode", "api_key") == "api_key"
              and direct_provider_route(name, row.get("api_base"))
              and row.get("api_key_source") is None and row.get("api_key_env") is None):
            # Only an unowned slot on the canonical endpoint is bound; admin keys/env names win.
            existing[name] = await repository.update_provider(row["id"], {"api_key_env": env_name})

    current = {(row["provider_id"], row["api_model_name"]) for row in await repository.list_models()}
    created = 0
    for model in planned:
        provider = existing.get(model["provider"])
        if provider is None or not provider.get("has_api_key"):
            continue
        identity = (provider["id"], api_model_name(model["model_name"], provider["provider_type"]))
        if identity in current:
            continue
        fields = {key: value for key, value in model.items() if key != "provider"}
        await repository.create_model(provider_id=provider["id"], **fields)
        current.add(identity)
        created += 1
    return {name: existing[name] for name in ENVIRONMENT_PROVIDERS if name in existing}, created


async def seed_environment_providers() -> dict[str, dict]:
    """Bootstrap environment-credentialed providers/models; return provider rows by name."""
    providers, _ = await _bootstrap()
    return providers


async def seed() -> None:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL must be set")
    init_db(database_url)
    try:
        providers, created = await _bootstrap()
    finally:
        await close_db()
    configured = sorted(name for name, row in providers.items() if row.get("has_api_key"))
    print(f"Lumen providers with credentials: {', '.join(configured) or 'none'}; models created: {created}", flush=True)


def main() -> None:
    asyncio.run(seed())


if __name__ == "__main__":
    main()
