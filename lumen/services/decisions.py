"""Single-attempt OpenAI Decisions, strict usage accounting and atomic quota holds."""
from __future__ import annotations

import asyncio
import json
import math
import uuid
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation

import httpx

from lumen.config import get_settings
from lumen.models.decision_contracts import DecisionRequest
from lumen.services import credit, provider_metadata, stateless_credit
from lumen.services.api_key_store import ApiKeyAuthorityUnavailable, ApiKeyForbidden
from lumen.services.inference_authority import authorize_new_io
from lumen.services.providers import errors, routing
from lumen.services.providers.credentials import resolve_route_api_key
from lumen.services.usage_breakdown import UsageBreakdown

# The published vision table does not specify gpt-6-luna's image multiplier.
# Use a conservative local admission ceiling, not a claimed official token count.
# https://developers.openai.com/api/docs/guides/images-vision#calculating-costs
IMAGE_TOKEN_CEILING = 1_000_000
MAX_IMAGES = 16
MAX_INPUT_TOKEN_BOUND = 32_000_000
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_TIMEOUT = httpx.Timeout(120.0, connect=10.0, read=120.0, write=30.0, pool=10.0)
_QUANTUM = Decimal("0.0000000001")
_RATE_KEYS = ("input_price_per_token", "cache_read_price_per_token", "cache_write_price_per_token")


class DecisionError(Exception):
    def __init__(self, status_code: int, message: str, *, uncertain: bool = False):
        self.status_code = status_code
        self.message = message
        self.uncertain = uncertain
        super().__init__(message)


async def resolve(model: str) -> dict:
    try:
        route = await routing.resolve_api_model(model, model_kind="text", required_capabilities=("decisions",))
    except errors.AmbiguousModelRouteError as exc:
        raise DecisionError(409, "model_route_ambiguous") from exc
    except errors.ChatStorageUnavailable as exc:
        raise DecisionError(503, "decision model configuration unavailable") from exc
    if route is None:
        raise DecisionError(404, "decision model not found")
    return route


def _validate_route(route: dict) -> None:
    if route.get("provider_type") != "openai" or route.get("model_kind", "text") != "text":
        raise DecisionError(422, "decisions requires an OpenAI-protocol text model")
    if (route.get("provider_auth") or {}).get("auth_mode", "api_key") != "api_key":
        raise DecisionError(422, "decisions requires API-key provider authentication")
    if (route.get("capabilities") or {}).get("decisions") is not True:
        raise DecisionError(422, "model decisions capability unavailable")
    _rate(route, "input_price_per_token", required=True)


def _rate(route: dict, key: str, *, required: bool = False) -> Decimal | None:
    raw = route.get(key)
    if raw is None:
        if required:
            raise DecisionError(422, f"decision pricing unavailable: {key}")
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise DecisionError(422, "decision pricing invalid") from exc
    if not value.is_finite() or value < 0:
        raise DecisionError(422, "decision pricing invalid")
    return value


def input_token_bound(body: DecisionRequest, route: dict) -> int:
    """Tokenizer estimate plus byte-safe text ceiling, framing and per-image cap."""
    payload = body.model_dump(exclude_none=True)
    images = 0

    def without_images(value):
        nonlocal images
        if isinstance(value, dict):
            if value.get("type") == "input_json":
                return value
            if value.get("type") == "input_image":
                images += 1
                return {"type": "input_image", "detail": value.get("detail", "auto")}
            return {key: without_images(item) for key, item in value.items()}
        if isinstance(value, list):
            return [without_images(item) for item in value]
        return value

    text = json.dumps(without_images(payload), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if images > MAX_IMAGES:
        raise DecisionError(422, "decisions supports at most 16 images per request")
    estimated = provider_metadata.count_tokens(route["model_name"], text=text)
    # One UTF-8 byte per token bounds tokenizer unknown/fallback estimates. The
    # complete wire skeleton includes role/choice/tool text and JSON object keys.
    bound = max(estimated, len(text.encode("utf-8"))) + 4096 + images * IMAGE_TOKEN_CEILING
    if bound > MAX_INPUT_TOKEN_BOUND:
        raise DecisionError(422, "decision input exceeds local token reservation ceiling")
    return bound


def credit_bound(route: dict, tokens: int) -> Decimal:
    rates = [_rate(route, key) for key in (*_RATE_KEYS, *(f"{key}_above_200k" for key in _RATE_KEYS))]
    rate = max((item for item in rates if item is not None), default=Decimal("0"))
    cost = rate * tokens
    multiplier = Decimal(str(route["margin_multiplier"])) * Decimal(str(get_settings().chat_credit_per_usd))
    if not multiplier.is_finite() or multiplier < 0:
        raise DecisionError(422, "decision credit conversion invalid")
    return ((cost + (_QUANTUM * 3 if cost else 0)) * multiplier).quantize(
        Decimal("0.00000001"), rounding=ROUND_CEILING,
    )


def validate_usage(raw: object) -> tuple[dict, UsageBreakdown]:
    try:
        if not isinstance(raw, dict):
            raise ValueError
        counts = [raw["input_tokens"], raw["output_tokens"], raw["total_tokens"],
                  raw["input_tokens_details"]["cached_tokens"],
                  raw["input_tokens_details"]["cache_write_tokens"],
                  raw["output_tokens_details"]["reasoning_tokens"]]
        if any(type(value) is not int or not 0 <= value <= 2147483647 for value in counts):
            raise ValueError
        inputs, outputs, total, cached, written, reasoning = counts
        if total != inputs + outputs or cached + written > inputs or reasoning > outputs:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise DecisionError(502, "invalid decision usage", uncertain=True) from exc
    usage = {"input_tokens": inputs, "output_tokens": outputs, "total_tokens": total,
             "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": written},
             "output_tokens_details": {"reasoning_tokens": reasoning}}
    return usage, UsageBreakdown(input_tokens=inputs, output_tokens=outputs,
                                 cache_read_input_tokens=cached, cache_creation_5m_input_tokens=written)


def decision_cost(route: dict, usage: dict) -> provider_metadata.UsageCost:
    inputs = usage["input_tokens"]
    cached = usage["input_tokens_details"]["cached_tokens"]
    written = usage["input_tokens_details"]["cache_write_tokens"]
    threshold = route.get("long_context_threshold_tokens")
    tiered = type(threshold) is int and threshold > 0 and inputs > threshold
    rates = {}
    for key, count in zip(_RATE_KEYS, (inputs - cached - written, cached, written), strict=True):
        tier = _rate(route, f"{key}_above_200k") if tiered else None
        rate = tier if tier is not None else _rate(route, key, required=bool(count) or key == _RATE_KEYS[0])
        rates[key] = rate
    costs = [(Decimal(count) * (rates[key] or Decimal("0"))).quantize(_QUANTUM, rounding=ROUND_HALF_UP)
             for key, count in zip(_RATE_KEYS, (inputs - cached - written, cached, written), strict=True)]
    snapshot = {key: str(rate) if rate is not None else None for key, rate in rates.items()}
    snapshot.update(category="decision", output_price_per_token="0", reasoning_price_per_token="0",
                    cache_write_ttl="30m", long_context_threshold_tokens=threshold,
                    long_context_applied=tiered, price_source=route.get("price_source"))
    return provider_metadata.UsageCost(raw_cost=sum(costs, Decimal("0")), input_cost=costs[0],
                                      output_cost=Decimal("0"), cache_read_cost=costs[1],
                                      cache_creation_5m_cost=costs[2], pricing_status="priced",
                                      pricing_snapshot=snapshot)


def _probability(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError
    return value


def validate_answers(raw: object, body: DecisionRequest) -> list[dict]:
    try:
        if not isinstance(raw, list) or len(raw) != len(body.questions):
            raise ValueError
        answers = []
        for answer, question in zip(raw, body.questions, strict=True):
            if not isinstance(answer, dict) or answer.get("name") != question.name:
                raise ValueError
            kind = answer["type"]
            clean = {"type": kind, "name": question.name}
            if kind == "refusal":
                answers.append(clean)
                continue
            if kind != question.type:
                raise ValueError
            if kind == "predicate":
                clean["probability"] = _probability(answer["probability"])
            else:
                clean["confidence"] = _probability(answer["confidence"])
                probabilities = answer["probabilities"]
                options = question.choices if kind == "choice" else question.levels
                if not isinstance(probabilities, list) or len(probabilities) != len(options):
                    raise ValueError
                result = []
                seen = set()
                for item in probabilities:
                    value = item["value"]
                    if kind == "choice":
                        key = (type(value), value)
                        allowed = {(type(option.value), option.value) for option in options}
                        if key not in allowed or key in seen:
                            raise ValueError
                        seen.add(key)
                        result.append({"value": value, "probability": _probability(item["probability"])})
                    else:
                        if type(value) is not int or not 0 <= value < len(options) or value in seen:
                            raise ValueError
                        if item["label"] != options[value].label:
                            raise ValueError
                        seen.add(value)
                        result.append({"value": value, "label": item["label"], "probability": _probability(item["probability"])})
                if abs(sum(item["probability"] for item in result) - 1) > 0.001:
                    raise ValueError
                clean["probabilities"] = result
                if kind == "choice":
                    choice = answer["choice"]
                    if (type(choice), choice) not in seen:
                        raise ValueError
                    clean["choice"] = choice
                else:
                    score = answer["score"]
                    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                        raise ValueError
                    if not 0 <= score <= len(options) - 1:
                        raise ValueError
                    clean["score"] = score
            answers.append(clean)
        return answers
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DecisionError(502, "invalid decision answers", uncertain=True) from exc


async def transport(route: dict, body: DecisionRequest) -> dict:
    key = await resolve_route_api_key(route)
    url = (route.get("api_base") or "https://api.openai.com/v1").rstrip("/") + "/decisions"
    payload = body.model_dump(exclude_none=True, exclude_unset=True)
    payload["model"] = route["api_model_name"]
    try:
        async with asyncio.timeout(120):
            async with (
                httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False, trust_env=False,
                                  transport=httpx.AsyncHTTPTransport(retries=0, trust_env=False)) as client,
                client.stream("POST", url, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                              json=payload) as response,
            ):
                if response.status_code != 200:
                    status = 429 if response.status_code == 429 else 502
                    raise DecisionError(status, "upstream decision request rejected", uncertain=response.status_code >= 500)
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise DecisionError(502, "decision response exceeds size limit", uncertain=True)
                    content.extend(chunk)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        raise DecisionError(502, "upstream decision connection failed") from exc
    except (httpx.HTTPError, TimeoutError) as exc:
        raise DecisionError(502, "upstream decision request failed", uncertain=True) from exc
    try:
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError
    except (ValueError, UnicodeError) as exc:
        raise DecisionError(502, "invalid decision response", uncertain=True) from exc
    return result


async def decide(*, route: dict, body: DecisionRequest, user_id: str, project_id: str,
                 api_key_id: int | None, required_scopes: tuple[str, ...], source: str = "api") -> dict:
    _validate_route(route)
    tokens = input_token_bound(body, route)
    bound = credit_bound(route, tokens)
    event_id = str(uuid.uuid4())
    reserved = sent = False
    try:
        await authorize_new_io(user_id=user_id, project_id=project_id, api_key_id=api_key_id,
                               required_scopes=required_scopes)
        await stateless_credit.reserve(event_id=event_id, user_id=user_id, project_id=project_id,
                                       api_key_id=api_key_id, bound=bound)
        reserved = True
        sent = True
        response = await transport(route, body)
        usage, breakdown = validate_usage(response.get("usage"))
        if usage["input_tokens"] > tokens:
            raise DecisionError(502, "decision usage exceeds admitted input ceiling", uncertain=True)
        try:
            cost = decision_cost(route, usage)
        except DecisionError as exc:
            raise DecisionError(502, "reported decision cache usage has no valid rate", uncertain=True) from exc
        answers = validate_answers(response.get("answers"), body)
        credited = await stateless_credit.settle(
            event_id=event_id, user_id=user_id, project_id=project_id, model_name=route["model_name"],
            provider=route.get("provider_name"), prompt_tokens=usage["input_tokens"],
            completion_tokens=usage["output_tokens"], usage_cost=cost,
            margin_multiplier=route["margin_multiplier"], source=source, api_key_id=api_key_id,
            breakdown=breakdown, execution_route=route,
            usage_components=[{"kind": "decision", "category": "decision", "unit": "request", "quantity": "1",
                               "cost_usd": str(cost.raw_cost), "input_tokens": usage["input_tokens"],
                               "output_tokens": usage["output_tokens"]}],
        )
        reserved = False
        return {"model": route["model_name"], "answers": answers, "usage": usage,
                "cost_usd": float(cost.raw_cost), "credited_cost": float(credited), "usage_log_id": event_id}
    except credit.QuotaExceeded as exc:
        raise DecisionError(429, str(exc)) from exc
    except ApiKeyForbidden as exc:
        raise DecisionError(403, "inference authority revoked") from exc
    except ApiKeyAuthorityUnavailable as exc:
        raise DecisionError(503, "inference authority unavailable") from exc
    except DecisionError as exc:
        sent = exc.uncertain
        raise
    except errors.ProviderSubscriptionError as exc:
        sent = False
        raise DecisionError(exc.status_code, "decision provider authentication unavailable") from None
    except errors.ProviderValidationError as exc:
        sent = False
        raise DecisionError(422, "decision provider configuration unavailable") from exc
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise DecisionError(503, "decision usage or service unavailable", uncertain=sent) from exc
    finally:
        if reserved:
            await asyncio.shield(stateless_credit.finish(event_id, uncertain=sent))
