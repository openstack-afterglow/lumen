"""Direct, single-attempt Gemini Decisions transport and input-only settlement."""
from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

import httpx

from lumen.config import get_settings
from lumen.models.billing import Payer
from lumen.models.gemini_decision_contracts import CONTEXT_TOKENS, GeminiDecisionRequest
from lumen.services import decisions, provider_metadata, stateless_credit
from lumen.services.durable_runs.api_completion import _REJECTION_STATUSES
from lumen.services.providers.credentials import resolve_route_api_key
from lumen.services.usage_breakdown import UsageBreakdown

API_URL = "https://generativelanguage.googleapis.com/v1beta/decisions"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_ERROR_BYTES = 8192
SAFETY_CODES = frozenset({"safety", "prohibited_content", "blocklist", "image_safety", "content_blocked"})
USD_QUANTUM = Decimal("0.0000000001")
CREDIT_QUANTUM = Decimal("0.00000001")
_ROUNDING_PER_OPTION = 0.005
_FLOAT_EPSILON = 1e-9


def _validate_route(route: dict) -> None:
    kind = route.get("model_kind", "text")
    enabled = kind == "decision" or (kind == "text" and (route.get("capabilities") or {}).get("decisions") is True)
    if not enabled or not provider_metadata.is_gemini_decision_model(
        route.get("api_model_name", ""), route.get("provider_type"),
    ):
        raise decisions.DecisionError(422, "decisions requires a Gemini decision model")
    if ((route.get("provider_auth") or {}).get("auth_mode", "api_key") != "api_key"
            or not provider_metadata.direct_provider_route("gemini", route.get("api_base"))):
        raise decisions.DecisionError(422, "Gemini decisions requires official API-key provider authentication")


def credit_bound(input_rate: Decimal, margin_multiplier: Decimal, credit_per_usd: Decimal) -> Decimal:
    # The Decision 1 card guarantees a maximum of 262,144 input tokens:
    # https://aistudio.google.com/docs/models/gemini-decision-1
    # Neither Decision 1 image token ceilings/default nor question framing is
    # documented. The approximate Gemini 4/3 resolution table is not a Decision 1
    # billing guarantee: https://aistudio.google.com/docs/media-resolution#token-counts
    # Reserve the whole documented window for every admitted request, not a guess.
    multiplier = margin_multiplier * credit_per_usd
    if not multiplier.is_finite() or multiplier < 0:
        raise decisions.DecisionError(422, "decision credit conversion invalid")
    ceiling_usd = (input_rate * CONTEXT_TOKENS).quantize(USD_QUANTUM, rounding=ROUND_CEILING)
    return (ceiling_usd * multiplier).quantize(CREDIT_QUANTUM, rounding=ROUND_CEILING)


def validate_usage(raw: object, *, has_image: bool) -> dict:
    malformed = decisions.DecisionError(502, "invalid decision usage", uncertain=True)
    if not isinstance(raw, dict):
        raise malformed
    inputs = raw.get("total_input_tokens")
    outputs = raw.get("total_output_tokens")
    total = raw.get("total_tokens")
    if type(inputs) is not int or type(outputs) is not int or type(total) is not int or outputs != 0 or total != inputs:
        raise malformed
    if inputs > CONTEXT_TOKENS:
        raise decisions.DecisionError(502, "decision usage exceeds admitted input ceiling", uncertain=True)
    allowed = {"text", "image"} if has_image else {"text"}
    counts = raw.get("input_tokens_by_modality")
    if not isinstance(counts, list) or len(counts) > len(allowed):
        raise malformed
    breakdown = []
    seen = set()
    summed = 0
    for item in counts:
        if not isinstance(item, dict):
            raise malformed
        modality, tokens = item.get("modality"), item.get("tokens")
        if (not isinstance(modality, str) or modality not in allowed or modality in seen
                or type(tokens) is not int or not 0 <= tokens <= inputs):
            raise malformed
        summed += tokens
        seen.add(modality)
        breakdown.append({"modality": modality, "tokens": tokens})
    if summed != inputs:
        raise malformed
    return {"total_input_tokens": inputs, "input_tokens_by_modality": breakdown,
            "total_output_tokens": 0, "total_tokens": inputs}


def _number(value: object, lo: float, hi: float) -> int | float:
    # Bounds precede float operations: even a large integer must not overflow.
    if type(value) not in (int, float) or not lo <= value <= hi:
        raise ValueError
    return value


def _probabilities(raw: object, keys) -> dict:
    if not isinstance(raw, dict) or raw.keys() != keys:
        raise ValueError
    clean = {key: _number(value, 0, 1) for key, value in raw.items()}
    # Google's score example sums to 1.01 after rounding to two decimals:
    # https://aistudio.google.com/docs/decisions-api#score
    # Each rounded value can be off by half a cent, but its true value must
    # remain in [0,1]. Require 1 to be inside the resulting sum interval.
    # This cannot excuse obvious overcounts by adding zero-valued options.
    total = math.fsum(clean.values())
    if not total:
        raise ValueError
    if total > 1:
        tolerance = math.fsum(min(p, _ROUNDING_PER_OPTION) for p in clean.values())
    else:
        tolerance = math.fsum(min(1 - p, _ROUNDING_PER_OPTION) for p in clean.values())
    if abs(total - 1) > tolerance + _FLOAT_EPSILON:
        raise ValueError
    return clean


def validate_answers(raw: object, body: GeminiDecisionRequest) -> dict:
    try:
        if not isinstance(raw, dict) or raw.keys() != body.questions.keys():
            raise ValueError
        answers = {}
        for key, question in body.questions.items():
            answer = raw[key]
            if not isinstance(answer, dict) or answer.get("type") != question.type:
                raise ValueError
            clean = {"type": question.type, "confidence": _number(answer.get("confidence"), 0, 1)}
            if question.type == "noul":
                clean["noul"] = _number(answer.get("noul"), 0.01, 0.99)
            elif question.type == "choice":
                probabilities = _probabilities(answer.get("probabilities"), question.criteria.keys())
                winner = answer.get("choice")
                if not isinstance(winner, str) or winner not in probabilities or probabilities[winner] != max(probabilities.values()):
                    raise ValueError
                clean.update(choice=winner, probabilities=probabilities)
            else:
                legend = {str(index): label for index, label in enumerate(question.criteria)}
                if answer.get("legend") != legend:
                    raise ValueError
                clean.update(score=_number(answer.get("score"), 0, len(legend) - 1), legend=legend,
                             probabilities=_probabilities(answer.get("probabilities"), legend.keys()))
            answers[key] = clean
        return answers
    except ValueError:
        raise decisions.DecisionError(502, "invalid decision answers; reported usage was billed") from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate upstream JSON key")
    return result


async def _read(response: httpx.Response, limit: int) -> bytearray:
    content = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=65536):
        if len(content) + len(chunk) > limit:
            raise ValueError("decision response exceeds size limit")
        content.extend(chunk)
    return content


async def _safety_code(response: httpx.Response) -> str | None:
    try:
        body = json.loads(await _read(response, MAX_ERROR_BYTES), object_pairs_hook=_unique_object)
    except (ValueError, RecursionError, httpx.HTTPError):
        return None
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and code in SAFETY_CODES else None


async def transport(route: dict, body: GeminiDecisionRequest, *, on_dispatch: Callable[[], None]) -> dict:
    key = await resolve_route_api_key(route)
    payload = body.model_dump(exclude_none=True)
    payload["model"] = route["api_model_name"]
    rejection = None
    try:
        async with (
            asyncio.timeout(120),
            httpx.AsyncClient(timeout=decisions._TIMEOUT, follow_redirects=False, trust_env=False,
                              transport=httpx.AsyncHTTPTransport(retries=0, trust_env=False)) as client,
        ):
            on_dispatch()
            async with client.stream("POST", API_URL, headers={"x-goog-api-key": key}, json=payload) as response:
                if response.status_code != 200:
                    status = response.status_code
                    rejection = decisions.DecisionError(
                        status if status in {400, 429} else 502, "upstream decision request rejected",
                        uncertain=status not in _REJECTION_STATUSES,
                    )
                    if status == 400:
                        code = await _safety_code(response)
                        if code is not None:
                            raise decisions.DecisionError(400, code)
                    raise rejection
                content = await _read(response, MAX_RESPONSE_BYTES)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        raise decisions.DecisionError(502, "upstream decision connection failed") from None
    except (httpx.HTTPError, TimeoutError):
        # A read timeout on an error body does not undo a known rejection.
        if rejection is not None:
            raise rejection from None
        raise decisions.DecisionError(502, "upstream decision request failed", uncertain=True) from None
    except ValueError:
        raise decisions.DecisionError(502, "decision response exceeds size limit", uncertain=True) from None
    try:
        result = json.loads(content, object_pairs_hook=_unique_object)
        if not isinstance(result, dict):
            raise ValueError
    except (ValueError, RecursionError):
        raise decisions.DecisionError(502, "invalid decision response", uncertain=True) from None
    return result


async def decide(*, route: dict, body: GeminiDecisionRequest, user_id: str, project_id: str,
                 api_key_id: int | None, required_scopes: tuple[str, ...], source: str = "api",
                 payer: Payer | None = None) -> dict:
    _validate_route(route)
    rate = decisions._rate(route, "input_price_per_token", required=True)
    margin = Decimal(str(route["margin_multiplier"]))
    credit_per_usd = Decimal(str(get_settings().chat_credit_per_usd))
    bound = credit_bound(rate, margin, credit_per_usd)

    async def account(event_id: str, response: dict) -> dict:
        usage = validate_usage(response.get("usage"), has_image=body.has_image)
        inputs = usage["total_input_tokens"]
        amount = (rate * inputs).quantize(USD_QUANTUM, rounding=ROUND_HALF_UP)
        cost = provider_metadata.UsageCost(
            raw_cost=amount, input_cost=amount, output_cost=Decimal(0), pricing_status="priced",
            pricing_snapshot={"category": "decision", "input_price_per_token": format(rate, "f"),
                              "price_source": route.get("price_source"), "provider_type": "gemini"},
        )
        await stateless_credit.settle(
            event_id=event_id, user_id=user_id, project_id=project_id, model_name=route["model_name"],
            provider=route.get("provider_name") or "gemini", prompt_tokens=inputs, completion_tokens=0,
            usage_cost=cost, margin_multiplier=margin, credit_per_usd=credit_per_usd, source=source,
            api_key_id=api_key_id, breakdown=UsageBreakdown(input_tokens=inputs, output_tokens=0),
            payer=payer,
            execution_route=route, usage_components=[{
                "kind": "decision", "category": "decision", "unit": "request", "quantity": "1",
                "cost_usd": format(amount, "f"), "input_tokens": inputs, "output_tokens": 0,
                "input_tokens_by_modality": usage["input_tokens_by_modality"],
            }],
        )
        return usage

    response, usage = await decisions.accounted_call(
        user_id=user_id, project_id=project_id, api_key_id=api_key_id, required_scopes=required_scopes,
        payer=payer,
        bound=bound, dispatch=lambda mark: transport(route, body, on_dispatch=mark), account=account,
    )
    return {"model": route["model_name"], "answers": validate_answers(response.get("answers"), body), "usage": usage}
