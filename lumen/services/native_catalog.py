"""Validated, offline public-provider catalog. No compatible endpoint consults this data."""
from __future__ import annotations

import copy
import functools
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from urllib.parse import urlsplit

NATIVE_PROVIDER_TYPES = frozenset({"openai", "anthropic", "gemini", "perplexity"})
_MODEL_KINDS = frozenset({"text", "image", "tts", "stt", "realtime", "embedding"})
_CAPABILITIES = frozenset({"vision", "tool_call", "reasoning", "prompt_caching", "structured_output", "web_search", "decisions"})
_OFFICIAL_HOSTS = {
    "openai": {"developers.openai.com", "platform.openai.com", "openai.com"},
    "anthropic": {"platform.claude.com", "docs.anthropic.com", "anthropic.com", "www.anthropic.com", "docs.claude.com"},
    "gemini": {"ai.google.dev", "aistudio.google.com", "cloud.google.com", "docs.cloud.google.com"},
    "perplexity": {"docs.perplexity.ai", "www.perplexity.ai", "perplexity.ai"},
}
_TOKEN_FIELDS = frozenset({"input", "output", "cache_read", "cache_write", "cache_write_long"})
_ENTRY_FIELDS = frozenset({"id", "aliases", "kind", "display_name", "status", "context_window", "max_output_tokens", "input_modalities", "output_modalities", "capabilities", "token_pricing", "media_pricing", "source_urls", "verified_at", "notes"})


def _object(value: object, allowed: set | frozenset, location: str, required=()) -> dict:
    if not isinstance(value, dict) or set(value) - allowed or set(required) - set(value):
        raise ValueError(f"{location}: invalid or unknown fields")
    return value


def _string(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location}: nonempty string required")
    return value


def _strings(value: object, location: str, *, nonempty=False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{location}: list required")
    for item in value:
        _string(item, location)
    if len(value) != len(set(value)):
        raise ValueError(f"{location}: duplicate values")
    return value


def _positive_integer(value: object, location: str, *, nullable=True) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{location}: positive integer required")


def _price(value: object, location: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        raise ValueError(f"{location}: finite nonnegative decimal string required")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{location}: invalid price") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{location}: invalid price")


def _sources(value: object, provider: str, location: str) -> None:
    for url in _strings(value, location, nonempty=True):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in _OFFICIAL_HOSTS[provider] or parsed.username or parsed.password:
            raise ValueError(f"{location}: official HTTPS source required")


def validate_catalog(value: object) -> dict:
    """Reject malformed source data rather than silently changing billing or capabilities."""
    root = _object(value, {"schema_version", "catalog_version", "providers"}, "catalog", {"schema_version", "catalog_version", "providers"})
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise ValueError("catalog: unsupported schema_version")
    _string(root["catalog_version"], "catalog_version")
    providers = _object(root["providers"], NATIVE_PROVIDER_TYPES, "providers")
    if not providers:
        raise ValueError("providers: at least one provider required")
    for provider, raw in providers.items():
        record = _object(raw, {"display_name", "pricing_sources", "models"}, provider, {"display_name", "pricing_sources", "models"})
        _string(record["display_name"], provider)
        _sources(record["pricing_sources"], provider, f"{provider}.pricing_sources")
        if not isinstance(record["models"], list):
            raise ValueError(f"{provider}.models: list required")
        identities: set[str] = set()
        for entry in record["models"]:
            entry = _object(entry, _ENTRY_FIELDS, f"{provider}.model", _ENTRY_FIELDS)
            model_id = _string(entry["id"], "id")
            names = [model_id, *_strings(entry["aliases"], "aliases")]
            if len(names) != len(set(names)) or identities.intersection(names):
                raise ValueError(f"{provider}/{model_id}: duplicate ID or alias")
            identities.update(names)
            if entry["kind"] not in _MODEL_KINDS or entry["status"] not in {"active", "preview", "deprecated"}:
                raise ValueError(f"{provider}/{model_id}: invalid kind or status")
            _string(entry["display_name"], "display_name")
            _positive_integer(entry["context_window"], "context_window")
            _positive_integer(entry["max_output_tokens"], "max_output_tokens")
            for key in ("input_modalities", "output_modalities"):
                if entry[key] is not None:
                    _strings(entry[key], key, nonempty=True)
            capabilities = _object(entry["capabilities"], _CAPABILITIES, "capabilities")
            if any(type(flag) is not bool for flag in capabilities.values()):
                raise ValueError("capabilities: boolean values required")
            token = entry["token_pricing"]
            if token is not None:
                token = _object(token, _TOKEN_FIELDS | {"cache_write_ttl", "cache_write_long_ttl", "long_context"}, "token_pricing")
                for key in _TOKEN_FIELDS:
                    _price(token.get(key), key)
                for key in ("cache_write_ttl", "cache_write_long_ttl"):
                    ttl = token.get(key)
                    if ttl is not None and (not isinstance(ttl, str) or not re.fullmatch(r"[1-9][0-9]*[smhd]", ttl)):
                        raise ValueError(f"{key}: invalid TTL")
                tier = token.get("long_context")
                if tier is not None:
                    tier = _object(tier, _TOKEN_FIELDS | {"threshold_tokens"}, "long_context", {"threshold_tokens"})
                    _positive_integer(tier["threshold_tokens"], "threshold_tokens", nullable=False)
                    for key in _TOKEN_FIELDS:
                        _price(tier.get(key), f"long_context.{key}")
            if entry["media_pricing"] is not None:
                # Import lazily: pricing itself consumes this loader at runtime.
                from lumen.services.providers.pricing import validate_media_pricing

                try:
                    validate_media_pricing(entry["kind"], entry["media_pricing"])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{provider}/{model_id}: invalid media_pricing: {exc}") from exc
            _sources(entry["source_urls"], provider, "source_urls")
            try:
                verified = date.fromisoformat(entry["verified_at"])
            except (ValueError, TypeError) as exc:
                raise ValueError("verified_at: ISO date required") from exc
            if verified.isoformat() != entry["verified_at"]:
                raise ValueError("verified_at: ISO date required")
            if entry["notes"] is not None and not isinstance(entry["notes"], str):
                raise ValueError("notes: string or null required")
    return copy.deepcopy(root)


@functools.cache
def native_catalog() -> dict:
    raw = files("lumen").joinpath("data/native_model_catalog.json").read_text(encoding="utf-8")
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result
    return validate_catalog(json.loads(raw, object_pairs_hook=unique_pairs))


def catalog_response(provider_type: str | None = None) -> dict:
    catalog = native_catalog()
    if provider_type is not None and provider_type not in NATIVE_PROVIDER_TYPES:
        raise ValueError("unsupported_provider_type: choose openai, anthropic, gemini or perplexity")
    return {
        "schema_version": catalog["schema_version"],
        "catalog_version": catalog["catalog_version"],
        "providers": [
            {"provider_type": key, **copy.deepcopy(record)}
            for key, record in catalog["providers"].items() if provider_type is None or key == provider_type
        ],
    }
