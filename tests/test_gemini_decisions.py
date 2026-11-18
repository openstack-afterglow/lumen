"""Gemini Decisions wire, billing, routing and one-attempt transport contracts."""
from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from lumen.api import decisions as native
from lumen.api.compat import decisions as compat
from lumen.auth import get_principal
from lumen.models.gemini_decision_contracts import CONTEXT_TOKENS, GeminiDecisionRequest
from lumen.service_authority import SERVICE_CAPABILITIES
from lumen.services import decisions, gemini_decisions, stateless_credit
from lumen.services.providers import routing

MODEL = "gemini/gemini-decision-1"
TEXT = {"type": "text", "text": "{\"item\":\"cracked mug\"}"}
IMAGE = {"type": "image", "mime_type": "image/png", "data": "iVBORw0KGgo="}
NOUL = {"type": "noul", "instructions": "Was it damaged?"}
CHOICE = {"type": "choice", "instructions": TEXT, "criteria": {"billing": "", "returns": "Damaged items"}}
SCORE = {"type": "score", "instructions": [TEXT], "criteria": ["Minor", "Severe"]}


def request(**changes):
    return {"model": MODEL, "input": [TEXT], "questions": {"damaged": NOUL, "queue": CHOICE, "severity": SCORE}, **changes}


def result(*, image=False, total=100):
    return {
        "model": MODEL,
        "answers": {
            "damaged": {"type": "noul", "noul": 0.99, "confidence": 0.99},
            "queue": {"type": "choice", "choice": "returns", "probabilities": {"billing": 0.04, "returns": 0.96}, "confidence": 0.96},
            "severity": {"type": "score", "score": 0.9, "legend": {"0": "Minor", "1": "Severe"},
                         "probabilities": {"0": 0.1, "1": 0.9}, "confidence": 0.9},
        },
        "usage": {"total_input_tokens": total, "total_output_tokens": 0, "total_tokens": total,
                  "input_tokens_by_modality": [{"modality": "text", "tokens": total - (50 if image else 0)}]
                  + ([{"modality": "image", "tokens": 50}] if image else [])},
    }


def route(**changes):
    return {"model_name": MODEL, "api_model_name": "gemini-decision-1", "model_kind": "decision",
            "provider_type": "gemini", "provider_name": "Gemini", "api_base": None,
            "margin_multiplier": Decimal("2"), "input_price_per_token": Decimal("0.0000001"),
            "price_source": "native_catalog", **changes}


def validate(**changes):
    return GeminiDecisionRequest.model_validate(request(**changes), strict=True)


@pytest.mark.parametrize("content", ["plain text", TEXT, [TEXT], [TEXT, IMAGE], IMAGE])
def test_wire_round_trip(content):
    wire = request(input=content)
    parsed = validate(input=content)
    assert parsed.model_dump(exclude_none=True) == wire


@pytest.mark.parametrize("qkey", ["", "a" * 65, "has.period", "é", "slash/key", "newline\n"])
def test_question_keys(qkey):
    with pytest.raises(ValidationError, match="invalid question key"):
        validate(questions={qkey: NOUL})


@pytest.mark.parametrize("key", ["", "a" * 129, " leading", "trailing ", "has\x00control", "has\u00a0space", "a\rb"])
def test_choice_keys(key):
    with pytest.raises(ValidationError):
        validate(questions={"choose": {**CHOICE, "criteria": {"valid": "", key: ""}}})


def test_accept_unicode_and_internal_ascii_space_in_choice_key():
    key = "\U0001f642" * 128
    parsed = validate(questions={"A_-9": {**CHOICE, "criteria": {key: "", "two words": "yes"}}})
    assert list(parsed.questions["A_-9"].criteria) == [key, "two words"]


@pytest.mark.parametrize("count", [0, 1, 1000, 1001])
def test_question_count(count):
    questions = {f"q{i}": NOUL for i in range(count)}
    if count in (1, 1000):
        assert len(validate(questions=questions).questions) == count
    else:
        with pytest.raises(ValidationError, match="1 to 1000"):
            validate(questions=questions)


@pytest.mark.parametrize("kind", ["choice", "score"])
@pytest.mark.parametrize("count", [1, 2, 255, 256])
def test_criteria_counts(kind, count):
    criteria = {str(i): "" for i in range(count)} if kind == "choice" else [str(i) for i in range(count)]
    questions = {"q": {"type": kind, "instructions": "Choose", "criteria": criteria}}
    if count in (2, 255):
        assert len(validate(questions=questions).questions["q"].criteria) == count
    else:
        with pytest.raises(ValidationError):
            validate(questions=questions)


@pytest.mark.parametrize("option", ["image/svg+xml", "image/jpg", "application/octet-stream", "application/pdf"])
def test_only_published_image_mimes(option):
    with pytest.raises(ValidationError):
        validate(input={**IMAGE, "mime_type": option})


@pytest.mark.parametrize("uri", [
    "http://example.test/a", "gs://bucket/object", "https://youtu.be/x", "https://www.youtube.com/x",
    "https://u:p@example.test/a", "https:///file",
    "https://generativelanguage.googleapis.com/v1beta/files/synthetic-file",
    "https://GenerativeLanguage.GOOGLEAPIS.com./v1beta/files/synthetic-file",
])
def test_image_uris_fail_closed(uri):
    with pytest.raises(ValidationError):
        validate(input={"type": "image", "mime_type": "image/png", "uri": uri})


@pytest.mark.parametrize("override", [{"uri": "https://example.test/a"}, {"data": "!a!!"}, {"data": "AA"},
                                       {"data": None}, {"resolution": "auto"}, {"resolution": None}])
def test_one_valid_image_source_and_resolution(override):
    with pytest.raises(ValidationError):
        validate(input={**IMAGE, **override})


@pytest.mark.parametrize("changes", [
    {"input": ""}, {"input": []}, {"input": {"arbitrary": "JSON is serialized before submitting"}},
    {"input": [dict(TEXT, text="")]}, {"questions": {"q": {**NOUL, "instructions": []}}},
    {"questions": {"q": {**NOUL, "instructions": IMAGE}}},
    {"questions": {"q": {**SCORE, "criteria": ["", "valid"]}}},
    {"questions": {"q": {**NOUL, "criteria": {"true_description": None}}}},
    {"questions": {"q": {**NOUL, "criteria": {"true_description": 1}}}},
    {"safety_identifier": "not part of this API"},
])
def test_invalid_shapes(changes):
    with pytest.raises(ValidationError):
        validate(**changes)


def test_invalid_question_fails_fast():
    with pytest.raises(ValidationError) as exc:
        validate(questions={f"q{i}": {"type": "no"} for i in range(1000)})
    assert exc.value.error_count() == 1


@pytest.fixture
def ledger(monkeypatch):
    reserve, settle, finish, authorize = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(stateless_credit, "reserve", reserve)
    monkeypatch.setattr(stateless_credit, "settle", settle)
    monkeypatch.setattr(stateless_credit, "finish", finish)
    monkeypatch.setattr(decisions, "authorize_new_io", authorize)
    monkeypatch.setattr(gemini_decisions, "get_settings", lambda: SimpleNamespace(chat_credit_per_usd=Decimal("10")))
    return SimpleNamespace(reserve=reserve, settle=settle, finish=finish, authorize=authorize)


async def call(*, selected=None, body=None):
    return await gemini_decisions.decide(route=selected or route(), body=body or validate(), user_id="u", project_id="p",
                                         api_key_id=3, required_scopes=("compat:completions:write",))


@pytest.mark.parametrize("resolution", ["text-only", None, "low", "medium", "high", "ultra_high"])
@pytest.mark.parametrize("rate", [Decimal("0.0000001"), Decimal("0.0000002"), Decimal(0)])
async def test_context_bound_and_only_actual_input_settlement(ledger, monkeypatch, resolution, rate):
    has_image = resolution != "text-only"
    image = IMAGE if resolution is None else {**IMAGE, "resolution": resolution}
    # Also exercise a signed HTTPS URL, passed through without fetching in Lumen.
    if resolution == "high":
        image = {"type": "image", "mime_type": "image/png", "uri": "https://example.test/img?sig=a%2Fb", "resolution": "high"}
    body = validate(input=[TEXT, image] if has_image else [TEXT])
    provider = AsyncMock(return_value=result(image=has_image))
    monkeypatch.setattr(gemini_decisions, "transport", provider)
    response = await call(selected=route(input_price_per_token=rate), body=body)
    assert response == result(image=has_image)
    assert ledger.reserve.call_args.kwargs["bound"] == rate * CONTEXT_TOKENS * 20
    settled = ledger.settle.call_args.kwargs
    assert settled["prompt_tokens"] == 100 and settled["completion_tokens"] == 0
    assert settled["usage_cost"].raw_cost == rate * 100
    assert settled["usage_cost"].pricing_status == "priced"
    assert settled["usage_components"][0]["input_tokens_by_modality"] == response["usage"]["input_tokens_by_modality"]
    assert "cracked mug" not in str(settled)
    ledger.finish.assert_not_called()
    provider.assert_awaited_once()


@pytest.mark.parametrize("usage_changes", [
    {"total_input_tokens": True}, {"total_output_tokens": 1}, {"total_output_tokens": 0.0}, {"total_tokens": 99},
    {"input_tokens_by_modality": [{"modality": "text", "tokens": True}]},
    {"input_tokens_by_modality": [{"modality": "text", "tokens": 99}]},
    {"input_tokens_by_modality": [{"modality": "image", "tokens": 100}]},
    {"input_tokens_by_modality": [{"modality": "text", "tokens": 50}] * 2},
    {"input_tokens_by_modality": [{"modality": "audio", "tokens": 100}]},
    result(total=CONTEXT_TOKENS + 1)["usage"],
])
async def test_inconsistent_usage_leaves_hold_uncertain(ledger, monkeypatch, usage_changes):
    response = result()
    response["usage"].update(usage_changes)
    monkeypatch.setattr(gemini_decisions, "transport", AsyncMock(return_value=response))
    with pytest.raises(decisions.DecisionError) as exc:
        await call()
    assert exc.value.status_code == 502 and exc.value.uncertain
    ledger.settle.assert_not_called()
    assert ledger.finish.call_args.kwargs == {"uncertain": True}


@pytest.mark.parametrize("key,changes", [
    ("damaged", {"noul": 0.001}), ("damaged", {"noul": 0.991}), ("damaged", {"confidence": True}),
    ("damaged", {"noul": float("nan")}), ("damaged", {"type": "choice"}),
    ("queue", {"choice": "billing"}), ("queue", {"probabilities": {"billing": 0.5, "returns": 0.9}}),
    ("queue", {"probabilities": {"returns": 1}}), ("queue", {"probabilities": {"billing": 0, "wrong": 1}}),
    ("queue", {"probabilities": {"billing": -0.1, "returns": 1.1}}),
    ("severity", {"score": 1.001}), ("severity", {"legend": {"0": "wrong", "1": "Severe"}}),
    ("severity", {"score": float("inf")}),
])
def test_rejects_invalid_answer_shapes(key, changes):
    answers = result()["answers"]
    answers[key].update(changes)
    with pytest.raises(decisions.DecisionError, match="invalid decision answers"):
        gemini_decisions.validate_answers(answers, validate())


async def test_usage_settles_before_bad_answers_are_rejected(ledger, monkeypatch):
    response = result()
    del response["answers"]["damaged"]
    monkeypatch.setattr(gemini_decisions, "transport", AsyncMock(return_value=response))
    with pytest.raises(decisions.DecisionError, match="invalid decision answers"):
        await call()
    ledger.settle.assert_awaited_once()
    ledger.finish.assert_not_called()


@pytest.mark.parametrize("change", [dict(input_price_per_token=None), dict(api_base="https://elsewhere.test/"),
                                     dict(api_model_name="gemini-flash"), dict(model_kind="text"),
                                     dict(model_kind="embedding"), dict(provider_type="openai")])
async def test_invalid_routes_never_reserve_or_go_upstream(ledger, monkeypatch, change):
    provider = AsyncMock()
    monkeypatch.setattr(gemini_decisions, "transport", provider)
    with pytest.raises(decisions.DecisionError) as exc:
        await call(selected=route(**change))
    assert exc.value.status_code == 422
    ledger.reserve.assert_not_called()
    provider.assert_not_called()


@pytest.fixture
def fake_http(monkeypatch):
    def install(status=200, payload=None, *, responder=None):
        calls = []
        pin = AsyncMock(return_value="synthetic-current-key")
        monkeypatch.setattr(gemini_decisions, "resolve_route_api_key", pin)

        def handler(request):
            calls.append(request)
            if responder is not None:
                return responder(request)
            return httpx.Response(status, json=payload, headers={"location": "/must-not-follow"})

        def make_transport(**kwargs):
            assert kwargs == {"retries": 0, "trust_env": False}
            return httpx.MockTransport(handler)

        monkeypatch.setattr(httpx, "AsyncHTTPTransport", make_transport)
        return calls, pin
    return install


async def test_google_http_shape_and_fresh_credential(ledger, fake_http):
    response = result(image=True)
    calls, pin = fake_http(200, {"answers": response["answers"], "usage": response["usage"]})
    selected = route(capabilities={"decisions": True}, model_kind="text")
    assert await call(selected=selected, body=validate(input=[TEXT, IMAGE], model=None, model_row_id=12)) == response
    assert len(calls) == 1 and str(calls[0].url) == gemini_decisions.API_URL
    assert calls[0].headers["x-goog-api-key"] == pin.return_value and "authorization" not in calls[0].headers
    assert json.loads(calls[0].content) == request(model="gemini-decision-1", input=[TEXT, IMAGE])
    pin.assert_awaited_once_with(selected)


@pytest.mark.parametrize("status,code", [
    *((400, code) for code in sorted(gemini_decisions.SAFETY_CODES)), (400, "invalid_argument"),
    (401, None), (403, None), (404, None), (413, None), (415, None), (422, None), (429, None),
    (302, None), (408, None), (503, None),
])
async def test_http_rejection_and_ambiguity_single_attempt(ledger, fake_http, status, code):
    calls, _ = fake_http(status, {"error": {"code": code, "message": "private upstream detail"}})
    with pytest.raises(decisions.DecisionError) as exc:
        await call()
    assert len(calls) == 1
    assert exc.value.status_code == (status if status in {400, 429} else 502)
    assert exc.value.message == (code if code in gemini_decisions.SAFETY_CODES else "upstream decision request rejected")
    assert ledger.finish.call_args.kwargs == {"uncertain": status not in decisions._REJECTION_STATUSES}
    ledger.settle.assert_not_called()


class _TimedOutBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"{"
        raise httpx.ReadTimeout("private upstream detail")


@pytest.mark.parametrize(("failure", "status", "uncertain", "detail"), [
    ("connect", 502, False, "upstream decision connection failed"),
    ("read", 502, True, "upstream decision request failed"),
    ("non-json", 502, True, "invalid decision response"),
    ("duplicate-keys", 502, True, "invalid decision response"),
    ("oversized", 502, True, "decision response exceeds size limit"),
    ("400-body-read", 400, False, "upstream decision request rejected"),
])
async def test_gemini_transport_failure_hold_outcomes(ledger, fake_http, monkeypatch, failure, status, uncertain, detail):
    if failure == "oversized":
        monkeypatch.setattr(gemini_decisions, "MAX_RESPONSE_BYTES", 16)

    def respond(request):
        if failure == "connect":
            raise httpx.ConnectError("private upstream detail", request=request)
        if failure == "read":
            return httpx.Response(200, stream=_TimedOutBody())
        if failure == "400-body-read":
            return httpx.Response(400, stream=_TimedOutBody())
        content = {
            "non-json": b"private upstream detail",
            "duplicate-keys": b'{"usage":null,"usage":{"total_input_tokens":1}}',
            "oversized": b'{"usage":"' + b"x" * 32 + b'"}',
        }
        return httpx.Response(200, content=content[failure])

    calls, _ = fake_http(responder=respond)
    with pytest.raises(decisions.DecisionError) as exc:
        await call()
    assert (exc.value.status_code, exc.value.uncertain, exc.value.message) == (status, uncertain, detail)
    assert len(calls) == 1
    ledger.reserve.assert_awaited_once()
    ledger.finish.assert_awaited_once()
    assert ledger.finish.call_args.kwargs == {"uncertain": uncertain}
    ledger.settle.assert_not_called()


def client(*, auth_type="api_key", scopes=("native:runs:write", "compat:completions:write")):
    app = FastAPI()
    app.include_router(native.router, prefix="/v1")
    app.include_router(compat.router, prefix="/v1")
    app.include_router(compat.google_router)
    app.dependency_overrides[get_principal] = lambda: {
        "auth_type": auth_type, "user_id": "u", "project_id": "p", "is_system_admin": False,
        "api_key_id": 3 if auth_type == "api_key" else None, "scopes": scopes,
        "roles": ["member", *SERVICE_CAPABILITIES], "source": "api",
    }
    return TestClient(app)


@pytest.mark.parametrize("path,auth_type,expected_scope", [
    ("/v1/chat/decisions", "keystone", "native:runs:write"), ("/v1beta/decisions", "api_key", "compat:completions:write"),
])
def test_native_branch_and_google_compat(path, auth_type, expected_scope, monkeypatch):
    resolve = AsyncMock(return_value=route())
    run = AsyncMock(return_value=result())
    old_run = AsyncMock()
    monkeypatch.setattr(decisions, "resolve", resolve)
    monkeypatch.setattr(gemini_decisions, "decide", run)
    monkeypatch.setattr(decisions, "decide", old_run)
    wire_model = MODEL if auth_type == "keystone" else "models/gemini-decision-1"
    response = client(auth_type=auth_type).post(path, json=request(model=wire_model))
    assert response.status_code == 200 and response.json() == result()
    if auth_type == "keystone":
        resolve.assert_awaited_once_with(MODEL)
    else:
        resolve.assert_awaited_once_with("gemini-decision-1", provider_type="gemini")
    assert run.call_args.kwargs["required_scopes"] == (expected_scope,)
    old_run.assert_not_called()


@pytest.mark.parametrize("path", ["/v1/chat/decisions", "/v1beta/decisions"])
def test_gemini_http_exact_row_accepts_alias_and_rejects_mismatch(path, monkeypatch):
    selected = route(model_id=12, provider_id=2)
    exact = AsyncMock(return_value=selected)
    legacy = AsyncMock(side_effect=routing.AmbiguousModelRouteError("duplicate model"))
    monkeypatch.setattr(routing, "resolve_model_by_id", exact)
    monkeypatch.setattr(routing, "resolve_api_model", legacy)
    run = AsyncMock(return_value=result())
    monkeypatch.setattr(gemini_decisions, "decide", run)
    http = client()
    assert http.post(path, json=request(model=None, model_row_id=12)).status_code == 200
    exact.assert_awaited_once_with(12, model_kind=("text", "decision"), required_capabilities=("decisions",))
    assert run.await_args.kwargs["route"]["provider_id"] == 2
    legacy.assert_not_called()
    assert http.post(path, json=request(model="models/gemini-decision-1", model_row_id=12)).status_code == 200
    wrong = http.post(path, json=request(model="different", model_row_id=12))
    assert (wrong.status_code, wrong.json()["detail"]) == (422, "model_selection_mismatch")
    assert http.post(path, json=request()).status_code == 409
    assert run.await_count == 2


@pytest.mark.parametrize("path", ["/v1/chat/decisions", "/v1beta/decisions"])
@pytest.mark.parametrize("kind", ["audio", "video", "document"])
def test_unsupported_modalities_have_fixed_detail(path, kind, monkeypatch):
    monkeypatch.setattr(decisions, "resolve", AsyncMock(return_value=route()))
    run = AsyncMock()
    monkeypatch.setattr(gemini_decisions, "decide", run)
    resp = client().post(path, json=request(input=[TEXT, {"type": kind, "data": "private"}]))
    assert resp.status_code == 422 and resp.json() == {"detail": "decision_input_modality_unsupported"}
    run.assert_not_called()


@pytest.mark.parametrize("path", ["/v1/chat/decisions", "/v1beta/decisions"])
def test_20_mib_body_limit_and_duplicate_depth_guards(path, monkeypatch):
    resolve = AsyncMock(return_value=route())
    run = AsyncMock(return_value=result())
    monkeypatch.setattr(decisions, "resolve", resolve)
    monkeypatch.setattr(gemini_decisions, "decide", run)
    http = client()
    headers = {"content-type": "application/json"}
    encoded = json.dumps(request()).encode()
    response = http.post(path, content=encoded, headers={**headers, "content-length": str(20 * 1024 * 1024 + 1)})
    assert response.status_code == 413
    resolve.assert_not_called()
    assert http.post(path, content=encoded[:-1] + b',"input":"duplicate"}', headers=headers).status_code == 422
    nested = b"[" * 101 + b"0" + b"]" * 101
    response = http.post(path, content=encoded[:-1] + b',"extra":' + nested + b"}", headers=headers)
    assert response.status_code == 422 and "100 levels" in response.json()["detail"]
    run.assert_not_called()
    assert http.post(path, json=request(input='"[escaped]"' * 101)).status_code == 200


@pytest.mark.parametrize("path", ["/v1/chat/decisions", "/v1beta/decisions"])
def test_chunked_body_is_bounded_without_content_length(path, monkeypatch):
    monkeypatch.setattr(native, "MAX_GEMINI_DECISION_BODY_BYTES", 64)
    resolve = AsyncMock()
    monkeypatch.setattr(decisions, "resolve", resolve)
    response = client().post(path, content=iter([b" " * 32] * 3), headers={"content-type": "application/json"})
    assert response.status_code == 413
    resolve.assert_not_called()


def test_decision_studio_is_explicit_and_gets_only_effective_input_price(monkeypatch):
    from lumen.api import conversations

    sample = {"id": 1, "model_name": MODEL, "api_model_name": "gemini-decision-1", "api_provider": "gemini",
              "provider_id": 8, "provider_type": "gemini", "display_name": "Gemini Decision 1",
              "effective_capabilities": {"decisions": True}, "effective_input_price_per_million": Decimal("0.10")}
    models = [
        {**sample, "model_kind": "decision"},
        {**sample, "id": 2, "model_kind": "text"},
        {**sample, "id": 3, "model_kind": "text", "model_name": "gemini/gemini-flash"},
    ]
    monkeypatch.setattr(conversations.repository, "list_models", AsyncMock(return_value=models))
    monkeypatch.setattr(conversations.repository, "list_providers", AsyncMock(return_value=[{
        "id": 8, "name": "Gemini", "has_api_key": True,
    }]))
    http = client(scopes=("models:read",))
    http.app.include_router(conversations.router, prefix="/v1")
    text_models = http.get("/v1/chat/models").json()
    eligible = http.get("/v1/chat/models?model_kind=decision").json()
    assert [m["id"] for m in text_models] == [3]
    assert "effective_input_price_per_million" not in text_models[0]
    assert [m["id"] for m in eligible] == [1]
    assert eligible[0]["effective_input_price_per_million"] == "0.10"


def test_published_google_answers_are_accepted_without_renormalizing():
    # Exact answer examples from https://aistudio.google.com/docs/decisions-api#question-types.
    answers = json.loads(r'''{
        "is_defect": { "type": "noul", "noul": 0.03, "confidence": 0.97 },
        "department": {
            "type": "choice",
            "choice": "billing",
            "probabilities": { "billing": 0.99, "shipping": 0.004, "other": 0.003, "technical": 0.002 },
            "confidence": 0.99
        },
        "severity": {
            "type": "score",
            "score": 1.12,
            "legend": {
                "0": "Cosmetic: appearance only, no lost functionality.",
                "1": "Workaround available: a task fails, but another way works.",
                "2": "Fully blocked: a task fails with no workaround."
            },
            "probabilities": { "0": 0.03, "1": 0.83, "2": 0.15 },
            "confidence": 0.83
        }
    }''')
    body = validate(questions={
        "is_defect": NOUL,
        "department": {**CHOICE, "criteria": dict.fromkeys(answers["department"]["probabilities"], "")},
        "severity": {**SCORE, "criteria": list(answers["severity"]["legend"].values())},
    })
    assert gemini_decisions.validate_answers(answers, body) == answers


def test_google_rejects_wrong_auth_or_scope(monkeypatch):
    resolve = AsyncMock()
    monkeypatch.setattr(decisions, "resolve", resolve)
    assert client(auth_type="keystone").post("/v1beta/decisions", json=request()).status_code == 401
    assert client(scopes=()).post("/v1beta/decisions", json=request()).status_code == 403
    resolve.assert_not_called()


@pytest.mark.parametrize(("auth", "scopes", "expected"), [
    ("keystone", ("compat:completions:write",), 401), ("api_key", (), 403),
])
def test_google_does_not_parse_json_before_authorizing(auth, scopes, expected, monkeypatch):
    decode = Mock(side_effect=AssertionError("Unauthorized request entered the JSON decoder"))
    monkeypatch.setattr(native, "_decision_json", decode)
    resp = client(auth_type=auth, scopes=scopes).post(
        "/v1beta/decisions", content=b'{"', headers={"content-type": "application/json"},
    )
    assert resp.status_code == expected
    decode.assert_not_called()


def test_openai_compat_explicitly_rejects_registered_gemini(monkeypatch):
    resolve = AsyncMock(side_effect=[None, route()])
    monkeypatch.setattr(routing, "resolve_api_model", resolve)
    response = client().post("/v1/decisions", json={"model": MODEL, "input": "text", "questions": [
        {"type": "predicate", "instructions": "yes?"},
    ]})
    assert response.status_code == 422
    assert "/v1beta/decisions" in response.json()["error"]["message"]
    assert [call.kwargs["model_kind"] for call in resolve.call_args_list] == ["text", "decision"]


def test_main_registers_google_host_route_and_security():
    from lumen.main import app

    path = app.openapi()["paths"]["/v1beta/decisions"]["post"]
    assert path["security"] == [{"APIKeyBearer": []}, {"XApiKey": []}, {"GoogleApiKey": []}]
    assert path["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/GeminiDecisionRequest",
    }
    schemas = app.openapi()["components"]["schemas"]
    assert {"GeminiDecisionRequest", "GeminiDecision_ChoiceQuestion"} <= schemas.keys()
    assert "model_row_id" in schemas["GeminiDecisionRequest"]["properties"]
    assert "model_row_id" in schemas["NativeDecisionRequest"]["properties"]
    assert "model_row_id" not in schemas["DecisionRequest"]["properties"]
