from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import contextmanager
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from lumen.api import decisions as native
from lumen.api.compat import decisions as compat
from lumen.auth import get_principal
from lumen.models.decision_contracts import DecisionRequest, InputJSON
from lumen.service_authority import SERVICE_CAPABILITIES
from lumen.services import credit, decisions, stateless_credit
from lumen.services.api_key_store import ApiKeyForbidden


def request(**changes):
    return {"model": "gpt-6-luna", "input": "Package is broken", "questions": [
        {"type": "predicate", "name": "damaged", "instructions": "Is it damaged?"}], **changes}


def route(**changes):
    return {"model_name": "gpt-6-luna", "api_model_name": "upstream-luna", "model_kind": "text",
            "provider_type": "openai", "provider_name": "test", "capabilities": {"decisions": True},
            "api_key": "synthetic-not-a-secret", "margin_multiplier": Decimal("2"),
            "input_price_per_token": Decimal("0.0000001"),
            "cache_read_price_per_token": Decimal("0.00000001"),
            "cache_write_price_per_token": Decimal("0.000000125"), **changes}


def usage(inputs=100, cached=20, written=10, outputs=0, reasoning=0):
    return {"input_tokens": inputs, "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": written},
            "output_tokens": outputs, "output_tokens_details": {"reasoning_tokens": reasoning},
            "total_tokens": inputs + outputs}


def response():
    return {"model": "untrusted-upstream-alias", "answers": [
        {"type": "predicate", "name": "damaged", "probability": 0.9}], "usage": usage()}


@pytest.fixture
def lifecycle(monkeypatch):
    effects = {name: AsyncMock() for name in ("authorize", "reserve", "finish", "settle")}
    effects["settle"].return_value = Decimal("0.001")
    monkeypatch.setattr(decisions, "authorize_new_io", effects["authorize"])
    for name in ("reserve", "finish", "settle"):
        monkeypatch.setattr(stateless_credit, name, effects[name])
    return effects


async def run(body=None, selected_route=None):
    return await decisions.decide(route=selected_route or route(), body=DecisionRequest.model_validate(body or request()),
                                  user_id="u", project_id="p", api_key_id=3,
                                  required_scopes=("compat:completions:write",))


@pytest.mark.parametrize("count, valid", [(0, False), (1, True), (400, True), (401, False)])
def test_question_count_boundaries(count, valid):
    body = request(questions=[{"type": "predicate", "instructions": ""} for _ in range(count)])
    if valid:
        assert len(DecisionRequest.model_validate(body).questions) == count
    else:
        with pytest.raises(ValidationError):
            DecisionRequest.model_validate(body)


@pytest.mark.parametrize("kind,field,count,valid", [
    ("choice", "choices", 1, False), ("choice", "choices", 2, True), ("choice", "choices", 255, True),
    ("choice", "choices", 256, False), ("score", "levels", 1, False), ("score", "levels", 2, True),
    ("score", "levels", 10, True), ("score", "levels", 11, False),
])
def test_choice_and_level_bounds(kind, field, count, valid):
    options = [{"value" if kind == "choice" else "label": str(index)} for index in range(count)]
    body = request(questions=[{"type": kind, "instructions": "", field: options}])
    if valid:
        DecisionRequest.model_validate(body)
    else:
        with pytest.raises(ValidationError):
            DecisionRequest.model_validate(body)


def test_typed_unique_choices_and_question_names():
    body = request(questions=[{"type": "choice", "instructions": "", "choices": [{"value": "true"}, {"value": True}]}])
    DecisionRequest.model_validate(body)
    body["questions"][0]["choices"][1]["value"] = "true"
    with pytest.raises(ValidationError, match="unique"):
        DecisionRequest.model_validate(body)
    with pytest.raises(ValidationError, match="unique"):
        DecisionRequest.model_validate(request(questions=request()["questions"] * 2))


@pytest.mark.parametrize("part", [
    {"type": "input_image", "file_id": "file-1"}, {"type": "input_audio", "data": "x"},
    {"type": "item_reference", "id": "i"}, {"type": "input_image", "image_url": "file:///etc/passwd"},
    {"type": "input_image", "image_url": "HTTPS://example.test/x"},
])
def test_unsupported_parts_rejected(part):
    with pytest.raises(ValidationError):
        DecisionRequest.model_validate(request(input=[{"role": "user", "content": [part]}]))


@pytest.mark.parametrize("role,part", [
    ("developer", {"type": "input_json", "data": {}}),
    ("system", {"type": "input_image", "image_url": "https://example.test/x"}),
    ("assistant", {"type": "input_text", "text": "x"}),
])
def test_role_content_restrictions(role, part):
    with pytest.raises(ValidationError):
        DecisionRequest.model_validate(request(input=[{"role": role, "content": [part]}]))


@pytest.mark.parametrize("url", ["data:image/png;base64,AA==", "http://example.test/x", "https://example.test/x"])
@pytest.mark.parametrize("detail", ["low", "high", "auto", "original", None])
def test_images_accept_official_url_and_detail(url, detail):
    body = request(input=[{"role": "user", "content": [{"type": "input_image", "image_url": url, "detail": detail}]}])
    assert DecisionRequest.model_validate(body).input[0].content[0].image_url == url


def test_tool_history_and_breakpoints():
    body = request(input=[
        {"role": "system", "content": [{"type": "input_text", "text": "evidence", "prompt_cache_breakpoint": {"mode": "explicit"}}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "checking"}, {"type": "refusal", "refusal": "no"}]},
        {"type": "function_call", "call_id": "c", "name": "get.data", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c", "output": "value"},
        {"type": "custom_tool_call", "call_id": "d", "name": "custom", "input": "value"},
        {"type": "custom_tool_call_output", "call_id": "d", "output": [{"type": "input_text", "text": "result"}]},
    ])
    DecisionRequest.model_validate(body)
    body["input"][-1]["call_id"] = "unknown"
    with pytest.raises(ValidationError, match="matching"):
        DecisionRequest.model_validate(body)


@pytest.mark.parametrize("change", [
    {"questions": [{"type": "predicate", "name": None, "instructions": ""}]},
    {"questions": [{"type": "predicate", "instructions": []}]},
    {"questions": [{"type": "predicate", "instructions": [{"type": "input_text", "text": "", "prompt_cache_breakpoint": {}}]}]},
    {"prompt_cache_options": {"ttl": "12h"}}, {"prompt_cache_key": "x" * 65},
    {"safety_identifier": "x" * 129}, {"model_id": 42},
])
def test_invalid_optional_fields(change):
    with pytest.raises(ValidationError):
        DecisionRequest.model_validate(request(**change))


def nested(depth):
    result = {}
    for _ in range(depth - 1):
        result = {"x": result}
    return result


def test_input_json_limits_and_literal_protocol_keys():
    InputJSON(data=nested(32))
    with pytest.raises(ValidationError, match="32 container"):
        InputJSON(data=nested(33))
    InputJSON(data={"x": [0] * 9998})
    with pytest.raises(ValidationError, match="10000 values"):
        InputJSON(data={"x": [0] * 9999})
    InputJSON(data={"x": "x" * (256 * 1024 - 8)})
    with pytest.raises(ValidationError, match="256 KiB"):
        InputJSON(data={"x": "x" * (256 * 1024 - 7)})
    with pytest.raises(ValidationError):
        InputJSON(data={"x": float("nan")})
    with pytest.raises(ValidationError):
        InputJSON(data=[1])


def test_image_bound_counts_only_wire_parts():
    plain = DecisionRequest.model_validate(request(input=[{"role": "user", "content": [
        {"type": "input_json", "data": {"type": "input_image", "image_url": "ordinary data"}}]}]))
    assert decisions.input_token_bound(plain) < decisions.IMAGE_TOKEN_CEILING
    images = [{"type": "input_image", "image_url": "https://example.test/x"}] * 16
    body = DecisionRequest.model_validate(request(input=[{"role": "user", "content": images}]))
    assert decisions.input_token_bound(body) >= decisions.IMAGE_TOKEN_CEILING * 16
    body = DecisionRequest.model_validate(request(input=[{"role": "user", "content": images + images[:1]}]))
    with pytest.raises(decisions.DecisionError, match="16 images"):
        decisions.input_token_bound(body)


@pytest.mark.parametrize("change", [{"provider_type": "anthropic"}, {"model_kind": "embedding"},
    {"capabilities": {"decisions": False}}, {"capabilities": {}}, {"input_price_per_token": None},
    {"provider_auth": {"auth_mode": "chatgpt_device"}}])
async def test_route_refusals_before_quota_or_upstream(change, lifecycle, monkeypatch):
    upstream = AsyncMock()
    monkeypatch.setattr(decisions, "transport", upstream)
    with pytest.raises(decisions.DecisionError):
        await run(selected_route=route(**change))
    lifecycle["reserve"].assert_not_called()
    upstream.assert_not_called()


def test_stored_override_survives_existing_capability_accessor():
    from types import SimpleNamespace

    from lumen.services.providers.pricing import _effective_capabilities

    capabilities, source = _effective_capabilities(
        SimpleNamespace(model_kind="text", model_name="gpt-6-luna", capabilities={"decisions": True}, capability_source="override"),
        "openai", api_key_configured=True,
    )
    assert capabilities["decisions"] is True and source == "override"


@pytest.mark.parametrize("inputs,cached,written,outputs,expected", [
    (100, 20, 10, 0, "0.00000845"), (100, 0, 0, 0, "0.00001"),
    (100, 20, 10, 900, "0.00000845"), (0, 0, 0, 0, "0"),
])
def test_billing_input_only(inputs, cached, written, outputs, expected):
    cost = decisions.decision_cost(route(), usage(inputs, cached, written, outputs, min(outputs, 5)))
    assert cost.raw_cost == Decimal(expected)
    assert cost.output_cost == 0
    assert cost.pricing_snapshot["cache_write_ttl"] == "30m"


@pytest.mark.parametrize("inputs,expected", [(272000, "0.02719845"), (272001, "0.0543971")])
def test_long_context_tier_threshold(inputs, expected):
    selected = route(long_context_threshold_tokens=272000,
                     input_price_per_token_above_200k="0.0000002",
                     cache_read_price_per_token_above_200k="0.00000002",
                     cache_write_price_per_token_above_200k="0.00000025")
    assert decisions.decision_cost(selected, usage(inputs)).raw_cost == Decimal(expected)


@pytest.mark.parametrize("key", ["cache_read_price_per_token", "cache_write_price_per_token"])
def test_unpriced_cache_usage_refuses(key):
    with pytest.raises(decisions.DecisionError, match="pricing unavailable"):
        decisions.decision_cost(route(**{key: None}), usage())


@pytest.mark.parametrize("mutate", [
    lambda u: u.update(input_tokens=True), lambda u: u.update(input_tokens=-1),
    lambda u: u.update(total_tokens=101), lambda u: u.update(output_tokens=1.5),
    lambda u: u["input_tokens_details"].update(cached_tokens=101),
    lambda u: u["input_tokens_details"].update(cache_write_tokens=-1),
    lambda u: u["output_tokens_details"].update(reasoning_tokens=1),
    lambda u: u.pop("input_tokens_details"),
])
def test_strict_usage_rejects_malformed_counts(mutate):
    raw = usage()
    mutate(raw)
    with pytest.raises(decisions.DecisionError, match="invalid decision usage"):
        decisions.validate_usage(raw)


async def test_authority_revocation_precedes_quota_and_upstream(lifecycle, monkeypatch):
    lifecycle["authorize"].side_effect = ApiKeyForbidden("revoked")
    upstream = AsyncMock()
    monkeypatch.setattr(decisions, "transport", upstream)
    with pytest.raises(decisions.DecisionError) as caught:
        await run()
    assert caught.value.status_code == 403
    lifecycle["reserve"].assert_not_called()
    upstream.assert_not_called()


@pytest.mark.parametrize("uncertain", [False, True])
async def test_failure_releases_or_retains_unknown(lifecycle, monkeypatch, uncertain):
    monkeypatch.setattr(decisions, "transport", AsyncMock(side_effect=decisions.DecisionError(502, "synthetic", uncertain=uncertain)))
    with pytest.raises(decisions.DecisionError):
        await run()
    lifecycle["finish"].assert_awaited_once()
    assert lifecycle["finish"].call_args.kwargs == {"uncertain": uncertain}
    lifecycle["settle"].assert_not_called()


async def test_cancel_after_send_retains_unknown(lifecycle, monkeypatch):
    async def cancel_after_dispatch(*_args, on_dispatch):
        on_dispatch()
        raise asyncio.CancelledError()

    monkeypatch.setattr(decisions, "transport", cancel_after_dispatch)
    with pytest.raises(asyncio.CancelledError):
        await run()
    assert lifecycle["finish"].call_args.kwargs == {"uncertain": True}


async def test_missing_cache_price_after_response_retains_unknown(lifecycle, monkeypatch):
    monkeypatch.setattr(decisions, "transport", AsyncMock(return_value=response()))
    with pytest.raises(decisions.DecisionError) as caught:
        await run(selected_route=route(cache_write_price_per_token=None))
    assert caught.value.status_code == 502
    assert lifecycle["finish"].call_args.kwargs == {"uncertain": True}


async def test_quota_failure_never_enters_provider(lifecycle, monkeypatch):
    lifecycle["reserve"].side_effect = credit.QuotaExceeded("quota")
    upstream = AsyncMock()
    monkeypatch.setattr(decisions, "transport", upstream)
    with pytest.raises(decisions.DecisionError) as caught:
        await run()
    assert caught.value.status_code == 429
    upstream.assert_not_called()


@contextmanager
def fake_provider(payload, status=200):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append((self.path, dict(self.headers), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if status == 302:
                self.send_header("Location", "/must-not-follow")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def test_real_synthetic_http_success_all_answer_types(lifecycle, monkeypatch):
    body = request(questions=[
        *request()["questions"],
        {"type": "choice", "instructions": "department", "choices": [{"value": "a"}, {"value": True}]},
        {"type": "score", "instructions": "severity", "levels": [{"label": "low"}, {"label": "high"}]},
        {"type": "predicate", "name": "refused", "instructions": "refuse"},
    ])
    payload = response()
    payload["answers"].extend([
        {"type": "choice", "name": None, "choice": True, "confidence": 0.8,
         "probabilities": [{"value": "a", "probability": 0.2}, {"value": True, "probability": 0.8}]},
        {"type": "score", "name": None, "score": 0.7, "confidence": 0.7,
         "probabilities": [{"value": 0, "label": "low", "probability": 0.3}, {"value": 1, "label": "high", "probability": 0.7}]},
        {"type": "refusal", "name": "refused"},
    ])
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    with fake_provider(payload) as (base, calls):
        result = await run(body, route(api_base=base))
    assert result["model"] == "gpt-6-luna"
    assert [a["type"] for a in result["answers"]] == ["predicate", "choice", "score", "refusal"]
    assert result["cost_usd"] == 0.00000845
    assert len(calls) == 1 and calls[0][0] == "/v1/decisions"
    assert calls[0][1]["Authorization"] == f"Bearer {route()['api_key']}"
    assert calls[0][2]["model"] == "upstream-luna"
    settled = lifecycle["settle"].call_args.kwargs
    assert settled["usage_components"][0]["category"] == "decision"
    assert "Package" not in str(settled) and "damaged" not in str(settled)
    lifecycle["finish"].assert_not_called()


@pytest.mark.parametrize("status", [201, 202, 302, 400, 401, 403, 404, 408, 409, 413, 415, 422, 429, 499, 503])
async def test_synthetic_http_errors_single_attempt_no_redirect(lifecycle, status):
    with fake_provider({"error": "private upstream body"}, status) as (base, calls):
        with pytest.raises(decisions.DecisionError) as caught:
            await run(selected_route=route(api_base=base))
    assert len(calls) == 1
    assert "private" not in caught.value.message
    assert lifecycle["finish"].call_args.kwargs["uncertain"] is (status not in decisions._REJECTION_STATUSES)


async def test_connection_failure_releases_hold(lifecycle):
    with fake_provider(response()) as (base, _calls):
        pass
    with pytest.raises(decisions.DecisionError):
        await run(selected_route=route(api_base=base))
    assert lifecycle["finish"].call_args.kwargs == {"uncertain": False}


def principal(auth_type="api_key", scopes=("compat:completions:write", "native:runs:write")):
    return {"auth_type": auth_type, "user_id": "u", "project_id": "p", "api_key_id": 3 if auth_type == "api_key" else None,
            "source": "api", "scopes": scopes, "roles": ["member", *SERVICE_CAPABILITIES], "is_system_admin": False}


def client(who=None):
    app = FastAPI()
    app.include_router(native.router, prefix="/v1")
    app.include_router(compat.router, prefix="/v1")
    app.dependency_overrides[get_principal] = lambda: who or principal()
    return TestClient(app)


@pytest.mark.parametrize("path,auth_type,native_result", [
    ("/v1/decisions", "api_key", False), ("/v1/chat/decisions", "keystone", True),
    ("/v1/chat/decisions", "api_key", True),
])
def test_compat_and_native_routes(path, auth_type, native_result, monkeypatch):
    result = {**response(), "cost_usd": 0.1, "credited_cost": 1, "usage_log_id": "event-id"}
    monkeypatch.setattr(decisions, "resolve", AsyncMock(return_value=route()))
    call = AsyncMock(return_value=result)
    monkeypatch.setattr(decisions, "decide", call)
    reply = client(principal(auth_type)).post(path, json=request())
    assert reply.status_code == 200
    assert reply.json() == (result if native_result else {key: result[key] for key in ("model", "answers", "usage")})
    assert call.call_args.kwargs["required_scopes"] == (("native:runs:write",) if native_result else ("compat:completions:write",))
    assert call.call_args.kwargs["source"] == ("api" if auth_type == "api_key" else "web")


def test_routes_reject_wrong_auth_or_scopes_before_resolve(monkeypatch):
    resolve = AsyncMock()
    monkeypatch.setattr(decisions, "resolve", resolve)
    assert client(principal("keystone")).post("/v1/decisions", json=request()).status_code == 401
    assert client(principal(scopes=())).post("/v1/decisions", json=request()).status_code == 403
    resolve.assert_not_called()


@pytest.mark.parametrize("path", ["/v1/decisions", "/v1/chat/decisions"])
def test_route_body_limit_before_provider(monkeypatch, path):
    monkeypatch.setattr(native, "MAX_DECISION_BODY_BYTES", 64)
    resolve = AsyncMock()
    monkeypatch.setattr(decisions, "resolve", resolve)
    result = client().post(path, content=json.dumps(request()), headers={"Content-Type": "application/json"})
    assert result.status_code == 413
    resolve.assert_not_called()


@pytest.mark.parametrize("path", ["/v1/decisions", "/v1/chat/decisions"])
def test_route_validation_error_before_provider(monkeypatch, path):
    resolve = AsyncMock()
    monkeypatch.setattr(decisions, "resolve", resolve)
    assert client().post(path, json=request(questions=[])).status_code == 422
    resolve.assert_not_called()


def test_main_registers_both_routes_and_openapi_scope():
    from lumen.main import app

    assert {"/v1/decisions", "/v1/chat/decisions"} <= {r.path for r in app.routes if hasattr(r, "path")}
    operation = app.openapi()["paths"]["/v1/decisions"]["post"]
    assert operation["security"] == [{"APIKeyBearer": []}, {"XApiKey": []}]


@pytest.mark.parametrize("bad", [float("nan"), -0.1, 1.1, True])
def test_answers_refuse_invalid_probabilities(bad):
    payload = response()["answers"]
    payload[0]["probability"] = bad
    with pytest.raises(decisions.DecisionError):
        decisions.validate_answers(payload, DecisionRequest.model_validate(request()))


async def test_transport_uses_fresh_credential_reference(monkeypatch):
    resolver = AsyncMock(return_value="synthetic-current-credential")
    monkeypatch.setattr(decisions, "resolve_route_api_key", resolver)
    with fake_provider(response()) as (base, calls):
        selected = route(api_base=base, provider_auth={"auth_mode": "api_key", "generation": 4})
        await decisions.transport(selected, DecisionRequest.model_validate(request()))
    resolver.assert_awaited_once_with(selected)
    assert calls[0][1]["Authorization"] == f"Bearer {resolver.return_value}"


async def test_oversized_upstream_response_keeps_unknown_hold(lifecycle, monkeypatch):
    monkeypatch.setattr(decisions, "MAX_RESPONSE_BYTES", 32)
    with fake_provider(response()) as (base, calls):
        with pytest.raises(decisions.DecisionError, match="size limit"):
            await run(selected_route=route(api_base=base))
    assert len(calls) == 1
    assert lifecycle["finish"].call_args.kwargs == {"uncertain": True}


@pytest.mark.parametrize("base,expected", [
    (None, "https://api.openai.com/v1/decisions"),
    ("https://api.openai.com", "https://api.openai.com/v1/decisions"),
    ("https://api.openai.com/", "https://api.openai.com/v1/decisions"),
    ("https://api.openai.com/v1", "https://api.openai.com/v1/decisions"),
    ("https://example.test/openai/v1", "https://example.test/openai/v1/decisions"),
    ("https://example.test", "https://example.test/decisions"),
])
async def test_transport_normalizes_only_official_openai_bases(monkeypatch, base, expected):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=response())

    def transport(**kwargs):
        assert kwargs == {"retries": 0, "trust_env": False}
        return httpx.MockTransport(handler)

    monkeypatch.setattr(decisions.httpx, "AsyncHTTPTransport", transport)
    await decisions.transport(route(api_base=base), DecisionRequest.model_validate(request()))
    assert len(calls) == 1
    assert calls[0].method == "POST"
    assert str(calls[0].url) == expected


@pytest.mark.parametrize("failure", ["cancel", "storage"])
async def test_credential_resolution_failure_releases_before_dispatch(lifecycle, monkeypatch, failure):
    error = asyncio.CancelledError() if failure == "cancel" else credit.ChatStorageUnavailable("synthetic store outage")
    monkeypatch.setattr(decisions, "resolve_route_api_key", AsyncMock(side_effect=error))
    transport = AsyncMock()
    monkeypatch.setattr(decisions.httpx, "AsyncHTTPTransport", transport)
    expected = asyncio.CancelledError if failure == "cancel" else decisions.DecisionError
    with pytest.raises(expected):
        await run()
    lifecycle["reserve"].assert_awaited_once()
    assert lifecycle["finish"].call_args.kwargs == {"uncertain": False}
    transport.assert_not_called()
    lifecycle["settle"].assert_not_called()


@pytest.mark.parametrize("path", ["/v1/decisions", "/v1/chat/decisions"])
def test_full_size_invalid_body_has_bounded_validation_cost(path):
    body = request(input=[{"type": "unknown", "payload": "do-not-echo-" + "x" * 69}] * 131072)
    encoded = json.dumps(body).encode()
    assert 14 * 1024 * 1024 < len(encoded) < native.MAX_DECISION_BODY_BYTES
    started = time.perf_counter()
    reply = client().post(path, content=encoded, headers={"Content-Type": "application/json"})
    assert time.perf_counter() - started < 5
    assert reply.status_code == 422
    assert len(reply.content) < 12000
    errors = reply.json()["detail"]
    assert len(errors) == 2
    assert all(set(error) == {"type", "loc", "msg"} for error in errors)
    assert b"do-not-echo" not in reply.content


def test_input_discriminator_fails_fast_on_first_invalid_item():
    with pytest.raises(ValidationError) as caught:
        DecisionRequest.model_validate(request(input=[{"type": "unknown"}] * 10))
    assert len(caught.value.errors()) == 2  # One string-arm error and the first invalid list item's tag error.
    for invalid in ([], {}, 1):
        with pytest.raises(ValidationError):
            DecisionRequest.model_validate(request(input=[{"type": invalid}]))


def _real_resolved_route(*, custom=False):
    from provider_fixtures import configured_provider

    from lumen.models.chat_db import LlmModel
    from lumen.services.providers import routing

    provider = configured_provider(
        id=1, name="decisions-provider", api_provider="decisions-provider", provider_type="openai",
        auth_mode="api_key", api_base="https://compatible.example.test/openai/v1" if custom else None,
        is_active=True, margin_multiplier=Decimal("1"),
    )
    provider.accounts[0].id = 2
    provider.accounts[0].provider_id = 1
    provider.accounts[0].credentials[0].id = 3
    provider.accounts[0].credentials[0].account_id = 2
    model = LlmModel(
        id=10, provider_id=1, model_name="gpt-6-luna", model_kind="text", is_active=True,
        input_price=Decimal("0.0000001") if custom else None, output_price=None,
        capabilities={"decisions": True} if custom else None,
        capability_source="override" if custom else None,
    )
    return routing._resolved_model(model, provider, required_capabilities=("decisions",))


@pytest.mark.parametrize("inputs,expected", [(100, "0.00000845"), (272000, "0.02719845"), (272001, "0.0543971")])
def test_real_catalog_route_uses_luna_30m_cache_write_and_long_context(inputs, expected):
    selected = _real_resolved_route()
    assert selected["capabilities"]["decisions"] is True
    assert selected["cache_write_price_per_token"] * 1000000 == Decimal("0.125")
    assert selected["cache_write_price_per_token_above_200k"] * 1000000 == Decimal("0.25")
    assert selected["long_context_threshold_tokens"] == 272000
    assert decisions.decision_cost(selected, usage(inputs)).raw_cost == Decimal(expected)


async def test_real_compatible_input_only_route_refuses_inherited_cache_write(lifecycle, monkeypatch):
    selected = _real_resolved_route(custom=True)
    assert selected["cache_price_sources"]["cache_creation_5m"].startswith("fallback_input:")
    assert selected["cache_write_price_per_token"] == selected["input_price_per_token"]
    payload = response()
    payload["usage"] = usage(cached=0, written=10)
    monkeypatch.setattr(decisions, "transport", AsyncMock(return_value=payload))
    with pytest.raises(decisions.DecisionError, match="no valid rate"):
        await run(selected_route=selected)
    lifecycle["settle"].assert_not_called()
    assert lifecycle["finish"].call_args.kwargs == {"uncertain": True}
    # The missing tier must not resurrect the inherited base rate above threshold.
    with pytest.raises(decisions.DecisionError, match="pricing unavailable"):
        decisions.decision_cost(selected, usage(inputs=272001, cached=0, written=10))


@pytest.mark.parametrize("kind", ["choice", "score"])
def test_official_empty_probabilities_are_accepted(kind):
    if kind == "choice":
        question = {"type": kind, "instructions": "", "choices": [{"value": "a"}, {"value": True}]}
        answer = {"type": kind, "name": None, "choice": True, "confidence": 0.9, "probabilities": []}
    else:
        question = {"type": kind, "instructions": "", "levels": [{"label": "low"}, {"label": "high"}]}
        answer = {"type": kind, "name": None, "score": 0.5, "confidence": 0.9, "probabilities": []}
    body = DecisionRequest.model_validate(request(questions=[question]))
    assert decisions.validate_answers([answer], body)[0]["probabilities"] == []


def test_rounded_255_choice_probabilities_are_not_rejected_by_sum():
    options = [{"value": str(index)} for index in range(255)]
    body = DecisionRequest.model_validate(request(questions=[{"type": "choice", "instructions": "", "choices": options}]))
    answer = {"type": "choice", "name": None, "choice": "0", "confidence": 0.004,
              "probabilities": [{"value": str(index), "probability": 0.004} for index in range(255)]}
    assert len(decisions.validate_answers([answer], body)[0]["probabilities"]) == 255


async def test_valid_priced_usage_settles_before_invalid_answers(lifecycle, monkeypatch):
    payload = response()
    payload["answers"] = []
    monkeypatch.setattr(decisions, "transport", AsyncMock(return_value=payload))
    with pytest.raises(decisions.DecisionError, match="invalid decision answers") as caught:
        await run()
    assert caught.value.status_code == 502
    lifecycle["settle"].assert_awaited_once()
    lifecycle["finish"].assert_not_called()


async def test_cleanup_failure_logs_event_without_masking_original_error(lifecycle, monkeypatch, caplog):
    original = decisions.DecisionError(429, "original provider rejection")
    monkeypatch.setattr(decisions, "transport", AsyncMock(side_effect=original))
    lifecycle["finish"].side_effect = credit.ChatStorageUnavailable("cleanup store unavailable")
    with pytest.raises(decisions.DecisionError) as caught:
        await run()
    assert caught.value is original
    event_id = lifecycle["reserve"].call_args.kwargs["event_id"]
    assert f"event_id={event_id}" in caplog.text


def test_byte_bound_does_not_tokenize(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("byte-safe quota bound must not invoke a tokenizer")

    monkeypatch.setattr(decisions.provider_metadata, "count_tokens", forbidden)
    body = DecisionRequest.model_validate(request(input="한국어 " * 100))
    text = json.dumps(body.model_dump(exclude_none=True), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    assert decisions.input_token_bound(body) == len(text.encode("utf-8")) + 4096


def _invalid_list_body(shape):
    if shape == "input_items":
        return request(input=[0] * 131072)
    if shape == "input_json_values":
        return request(input=[{"role": "user", "content": [{"type": "input_json", "data": [0] * 10000}]}])
    if shape == "message_parts":
        return request(input=[{"role": "user", "content": [0] * 16384}] * 16)
    return request(questions=[{"type": "predicate", "instructions": [0] * 16384}] * 16)


@pytest.mark.parametrize("shape", ["input_items", "input_json_values", "message_parts", "question_parts"])
def test_invalid_list_validation_fails_fast_with_small_error_count(shape):
    body = _invalid_list_body(shape)
    started = time.perf_counter()
    with pytest.raises(ValidationError) as caught:
        DecisionRequest.model_validate(body)
    errors = caught.value.errors(include_input=False, include_url=False, include_context=False)
    assert time.perf_counter() - started < 2
    assert 1 <= len(errors) <= 4
    assert len(json.dumps(errors).encode()) < 4000


@pytest.mark.parametrize("path", ["/v1/decisions", "/v1/chat/decisions"])
@pytest.mark.parametrize("shape", ["input_items", "input_json_values", "message_parts", "question_parts"])
def test_invalid_list_http_validation_has_bounded_time_and_size(path, shape, monkeypatch):
    encoded = json.dumps(_invalid_list_body(shape), separators=(",", ":")).encode()
    resolve = AsyncMock()
    monkeypatch.setattr(decisions, "resolve", resolve)
    started = time.perf_counter()
    reply = client().post(path, content=encoded, headers={"Content-Type": "application/json"})
    assert time.perf_counter() - started < 5
    assert reply.status_code == 422
    assert 1 <= len(reply.json()["detail"]) <= 4
    assert len(reply.content) < 4000
    resolve.assert_not_called()


def test_input_json_wide_nested_values_stop_without_materializing_all_children():
    started = time.perf_counter()
    with pytest.raises(ValidationError, match="10000 values"):
        InputJSON(data={"wide": [0] * 100000})
    assert time.perf_counter() - started < 2


def test_valid_mixed_official_request_round_trips_with_fail_fast_lists():
    body = request(input=[
        {"role": "developer", "content": [{"type": "input_text", "text": "Rules"}]},
        {"role": "user", "content": [
            {"type": "input_text", "text": "Evidence", "prompt_cache_breakpoint": {"mode": "explicit"}},
            {"type": "input_json", "data": [{"order": {"damaged": True}}]},
            {"type": "input_image", "image_url": "https://example.test/image.png", "detail": "original"},
        ]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "Checking"}, {"type": "refusal", "refusal": "No"}]},
        {"type": "function_call", "name": "lookup", "call_id": "fn", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "fn", "output": [{"type": "input_text", "text": "Found"}]},
        {"type": "custom_tool_call", "name": "check", "call_id": "custom", "input": "Evidence"},
        {"type": "custom_tool_call_output", "call_id": "custom", "output": [{"type": "input_image", "image_url": "data:image/png;base64,AA=="}]},
    ], questions=[
        {"type": "predicate", "name": "damaged", "instructions": [
            {"type": "input_text", "text": "Is it damaged?"}, {"type": "input_json", "data": {"rule": True}},
        ]},
        {"type": "choice", "instructions": "Route", "choices": [{"value": "true"}, {"value": True}]},
        {"type": "score", "instructions": "Severity", "levels": [{"label": "low"}, {"label": "high"}]},
    ], prompt_cache_options={"mode": "explicit", "ttl": "30m"})
    assert DecisionRequest.model_validate(body).model_dump(exclude_unset=True) == body


def test_every_request_list_schema_is_fail_fast():
    lists = []

    def visit(schema):
        if isinstance(schema, dict):
            if schema.get("type") == "list":
                lists.append(schema)
            for value in schema.values():
                visit(value)
        elif isinstance(schema, list):
            for value in schema:
                visit(value)

    visit(DecisionRequest.__pydantic_core_schema__)
    assert len(lists) >= 9
    assert all(schema.get("fail_fast") is True for schema in lists)


def _unknown_protocol_key_body(level, count):
    body = request()
    if level == "root":
        target = body
    else:
        body["input"] = [{"role": "user", "content": "Evidence"}]
        target = body["input"][0]
    target.update({f"unknown_{index:06}": "do-not-echo-" + "x" * 100 for index in range(count)})
    return body, target


@pytest.mark.parametrize("level", ["root", "message"])
def test_many_unknown_protocol_keys_fail_before_error_accumulation(level):
    body, _target = _unknown_protocol_key_body(level, 10000)
    started = time.perf_counter()
    with pytest.raises(ValidationError) as caught:
        DecisionRequest.model_validate(body)
    errors = caught.value.errors(include_input=False, include_url=False, include_context=False)
    assert time.perf_counter() - started < 2
    assert 1 <= len(errors) <= 2
    assert len(json.dumps(errors).encode()) < 1000


@pytest.mark.parametrize("path", ["/v1/decisions", "/v1/chat/decisions"])
@pytest.mark.parametrize("level", ["root", "message"])
def test_full_16mib_many_unknown_keys_http_cost_is_bounded(path, level, monkeypatch):
    body, target = _unknown_protocol_key_body(level, 100000)
    target["_padding"] = ""
    initial = json.dumps(body, separators=(",", ":")).encode()
    assert 12 * 1024 * 1024 < len(initial) < native.MAX_DECISION_BODY_BYTES
    target["_padding"] = "p" * (native.MAX_DECISION_BODY_BYTES - len(initial))
    encoded = json.dumps(body, separators=(",", ":")).encode()
    assert len(encoded) == native.MAX_DECISION_BODY_BYTES
    resolve = AsyncMock()
    monkeypatch.setattr(decisions, "resolve", resolve)
    started = time.perf_counter()
    reply = client().post(path, content=encoded, headers={"Content-Type": "application/json"})
    assert time.perf_counter() - started < 5
    assert reply.status_code == 422
    assert 1 <= len(reply.json()["detail"]) <= 2
    assert len(reply.content) < 1000
    assert b"do-not-echo" not in reply.content
    resolve.assert_not_called()


def test_protocol_key_guard_leaves_arbitrary_input_json_keys_untouched():
    data = {f"unknown_{index}": index for index in range(1000)}
    data.update(type="arbitrary", role="not-a-message", file_id="ordinary-data", audio={"opaque": True})
    body = request(input=[{"role": "user", "content": [{"type": "input_json", "data": data}]}])
    parsed = DecisionRequest.model_validate(body)
    assert parsed.input[0].content[0].data == data
    assert parsed.model_dump(exclude_unset=True) == body
