"""Explicit administrator-only, single-send checks of an exact stored credential."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from time import monotonic

from sqlalchemy import select

from lumen.models.chat_db import LlmModel
from lumen.services import provider_client
from lumen.services.provider_errors import AuthenticationError, PermissionDeniedError, ProviderError, Timeout

from . import accounts, routing
from .errors import (
    ProviderConfigurationChangedError,
    ProviderNotFoundError,
    ProviderSubscriptionError,
    ProviderValidationError,
)

_TIMEOUT_SECONDS = 30
_MAX_TOKENS = 32
_PONG_FORMAT = {"type": "json_schema", "json_schema": {"name": "connection_probe", "strict": True,
    "schema": {"type": "object", "properties": {"response": {"type": "string", "enum": ["pong"]}},
               "required": ["response"], "additionalProperties": False}}}


async def _authorize_admin(principal: dict) -> None:
    from lumen.auth import _refresh_token_authority, require_admin

    await _refresh_token_authority(principal)
    require_admin(principal)


async def _route(provider_id: int, account_id: int, credential_id: int, model_id: int, generation: int) -> dict:
    async with routing._require_db()() as session, session.begin():
        provider, account, credential = await accounts.lock_credential(
            session, provider_id, account_id=account_id, credential_id=credential_id)
        model = (await session.execute(select(LlmModel).where(
            LlmModel.id == model_id, LlmModel.provider_id == provider_id,
            LlmModel.model_kind == "text", LlmModel.is_active.is_(True)))).scalar_one_or_none()
        if model is None:
            raise ProviderNotFoundError("registered active text model not found for provider")
        if not provider.is_active or credential.generation != generation:
            raise ProviderConfigurationChangedError("provider credential changed; refresh before testing")
        ref = routing._provider_auth_ref(provider, account, credential, model_id=model_id, required_capabilities=("chat",))
        route = routing._resolved_model(model, provider, ref=ref)
        if route["capabilities"].get("output_budget_enforced") is not True:
            raise ProviderValidationError("selected model transport cannot enforce the probe output budget")
        return route


async def _current(route: dict) -> None:
    if await routing.resolve_model_snapshot(route) is None:
        raise ProviderConfigurationChangedError("provider credential or model changed; refresh before testing")


async def _rejected_current(route: dict) -> None:
    # Transport may mark this exact key reauth_required. That is the test outcome,
    # not permission to substitute a key; still reject concurrent rotation/revoke.
    ref = route["provider_auth"]
    async with routing._require_db()() as session, session.begin():
        provider, account, credential = await accounts.lock_credential(
            session, ref["provider_id"], account_id=ref["account_id"], credential_id=ref["credential_id"])
        model = (await session.execute(select(LlmModel).where(
            LlmModel.id == ref["model_id"], LlmModel.provider_id == ref["provider_id"],
            LlmModel.model_kind == "text", LlmModel.is_active.is_(True)))).scalar_one_or_none()
        if (model is None or model.model_name != route["model_name"]
                or provider.provider_type != route["provider_type"]
                or (provider.api_base if provider.auth_mode == "api_key" else None) != route["api_base"]):
            raise ProviderConfigurationChangedError("provider credential or model changed; refresh before testing")
        if (not provider.is_active or provider.auth_mode != ref["auth_mode"]
                or credential.auth_mode != ref["auth_mode"] or credential.generation != ref["generation"]
                or not accounts._eligible(account, ref["model_id"], ref["required_capabilities"])
                or not accounts._eligible(credential, ref["model_id"], ref["required_capabilities"])):
            raise ProviderConfigurationChangedError("provider credential changed; refresh before testing")


def _text(result, *, allow_length: bool = False) -> str | None:
    if not isinstance(result, dict):
        return None
    choices = result.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return None
    choice = choices[0]
    message = choice.get("message")
    terminals = {"stop", "length"} if allow_length else {"stop"}
    if choice.get("finish_reason") not in terminals or not isinstance(message, dict) or message.get("refusal") or message.get("tool_calls"):
        return None
    content = message.get("content")
    return content.strip() if isinstance(content, str) and content.strip() else None


async def test_connection(provider_id: int, account_id: int, credential_id: int, *, model_id: int,
                          generation: int, principal: dict) -> dict:
    route = await _route(provider_id, account_id, credential_id, model_id, generation)
    mode = "structured" if route["capabilities"].get("structured_output") is True else "plain"
    await _current(route)
    await _authorize_admin(principal)
    started = monotonic()
    status, reason = "failed", "provider_error"
    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS):
            result = await provider_client.acompletion(
                model=route["model_name"], messages=[{"role": "user", "content":
                    '{"request":"ping"}' if mode == "structured" else "ping"}],
                api_base=route["api_base"], api_key=None, custom_llm_provider=route["provider_type"],
                provider_auth=route["provider_auth"], max_tokens=_MAX_TOKENS,
                extra={"response_format": _PONG_FORMAT} if mode == "structured" else None)
        text = _text(result, allow_length=mode == "plain")
        valid = bool(text)
        if mode == "structured":
            try:
                valid = json.loads(text) == {"response": "pong"} if text else False
            except (ValueError, TypeError):
                valid = False
        status, reason = ("connected", None) if valid else ("failed", "invalid_response")
    except (AuthenticationError, PermissionDeniedError):
        reason = "auth_rejected"
    except (Timeout, TimeoutError):
        reason = "timeout"
    except ProviderSubscriptionError:
        reason = "auth_rejected"
    except (ProviderError, ValueError):
        # No response/body/error text is ever returned; no second request is made.
        reason = "provider_error"
    if reason == "auth_rejected":
        await _rejected_current(route)
    else:
        await _current(route)
    await _authorize_admin(principal)
    return {"status": status, "reason": reason, "mode": mode, "elapsed_ms": max(0, round((monotonic() - started) * 1000)),
            "model_id": model_id, "generation": generation, "checked_at": datetime.now(UTC)}
