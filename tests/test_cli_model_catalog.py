"""Coding-CLI catalog and exact route identities through the real routing queries.

Provider rows live in an in-memory catalog; provider transports and credit
accounting are the only synthetic boundaries, so no paid inference runs.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from lumen.auth import get_principal
from lumen.main import app
from lumen.models.chat_db import LlmModel, LlmProvider
from lumen.service_authority import SERVICE_CAPABILITIES
from lumen.services import completion_api as core
from lumen.services import litellm_client
from lumen.services.providers import routing
from lumen.services.providers.errors import AmbiguousModelRouteError, ChatStorageUnavailable

_H = {"Authorization": "Bearer sk-afgl-test"}
_SECRETS = ("sealed-key-1", "sealed-key-2", "sealed-key-6", "sealed-subscription", "key-1", "key-2")

pytestmark = pytest.mark.usefixtures("synthetic_inference_store")


class _Session(AbstractAsyncContextManager):
    def __init__(self, sync):
        self.sync = sync

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.sync.close()
        return False

    async def execute(self, statement):
        return self.sync.execute(statement)


def _per_token(per_million: str) -> Decimal:
    return Decimal(per_million) / Decimal("1000000")


@pytest.fixture(autouse=True)
def _allow_all_hosts(monkeypatch):
    monkeypatch.setattr("lumen.auth.get_settings", lambda: SimpleNamespace(chat_api_hosts=""))


@pytest.fixture
def catalog(monkeypatch):
    """Two connections expose the same public ID; inactive, unpriced and subscription routes surround them."""
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    LlmProvider.__table__.create(engine)
    LlmModel.__table__.create(engine)
    sync_factory = sessionmaker(engine, expire_on_commit=False)
    providers = [
        dict(id=1, name="OpenAI Direct", provider_type="openai", api_provider="openai", sort_order=1,
             encrypted_api_key="sealed-key-1"),
        dict(id=2, name="NVIDIA Gateway", provider_type="openai", api_provider="nvidia", sort_order=0,
             encrypted_api_key="sealed-key-2", api_base="https://gateway.invalid/v1"),
        dict(id=3, name="Claude Plan", provider_type="anthropic", api_provider="claude-plan", sort_order=2,
             auth_mode="anthropic_subscription", encrypted_subscription_tokens="sealed-subscription",
             subscription_status="configured"),
        dict(id=4, name="ChatGPT Plan", provider_type="chatgpt", api_provider="chatgpt", sort_order=3,
             auth_mode="chatgpt_device", encrypted_subscription_tokens="sealed-subscription",
             subscription_status="configured"),
        dict(id=5, name="Keyless", provider_type="openai", api_provider="keyless", sort_order=4),
        dict(id=6, name="Retired", provider_type="openai", api_provider="retired", sort_order=5,
             encrypted_api_key="sealed-key-6", is_active=False),
    ]
    priced = {"input_price": _per_token("1.25"), "output_price": _per_token("10"), "price_source": "manual"}
    models = [
        dict(id=11, provider_id=1, model_name="shared-coder", sort_order=1, **priced),
        dict(id=12, provider_id=1, model_name="unpriced-coder", sort_order=2),
        dict(id=13, provider_id=1, model_name="retired-coder", sort_order=0, is_active=False, **priced),
        dict(id=14, provider_id=1, model_name="shared-image", sort_order=0, model_kind="image", **priced),
        dict(id=15, provider_id=1, model_name="no-tools-coder", sort_order=3, capabilities={"tool_call": False},
             capability_source="override", **priced),
        dict(id=16, provider_id=1, model_name="nonstream-coder", sort_order=4, capabilities={"streaming": False},
             capability_source="override", **priced),
        dict(id=17, provider_id=1, model_name="image-output-only", sort_order=5,
             capabilities={"modalities": {"input": ["text"], "output": ["image"]}},
             capability_source="override", **priced),
        dict(id=18, provider_id=1, model_name="unknown-modalities-coder", sort_order=6,
             capabilities={"modalities": {"input": [], "output": []}},
             capability_source="override", **priced),
        dict(id=19, provider_id=1, model_name="image-input-only", sort_order=7,
             capabilities={"modalities": {"input": ["image"], "output": ["text"]}},
             capability_source="override", **priced),
        dict(id=20, provider_id=1, model_name="null-modalities-coder", sort_order=8,
             capabilities={"input_modalities": None, "output_modalities": None},
             capability_source="override", **priced),
        dict(id=21, provider_id=2, model_name="shared-coder", display_name="Shared Coder (gateway)",
             input_price=_per_token("0.5"), output_price=_per_token("2"), price_source="manual"),
        dict(id=31, provider_id=3, model_name="claude-plan-model", **priced),
        dict(id=41, provider_id=4, model_name="gpt-plan-model", **priced),
        dict(id=51, provider_id=5, model_name="keyless-coder", **priced),
        dict(id=61, provider_id=6, model_name="shared-coder", **priced),
    ]
    with sync_factory.begin() as session:
        session.add_all([LlmProvider(margin_multiplier=Decimal("1"), **row) for row in providers])
    with sync_factory.begin() as session:
        session.add_all([LlmModel(**row) for row in models])

    monkeypatch.setattr(routing, "_require_db", lambda: lambda: _Session(sync_factory()))
    # Stored values are sentinels, not ciphertext: only execution may read a credential.
    monkeypatch.setattr(
        routing, "resolve_api_key", lambda provider: f"key-{provider.id}" if provider.auth_mode == "api_key" else None
    )
    yield sync_factory
    engine.dispose()


@pytest.fixture
def api_key_principal(monkeypatch):
    def install(scopes=("models:read", "compat:completions:write"), auth_type="api_key"):
        async def principal():
            return {
                "auth_type": auth_type,
                "user_id": "u1",
                "project_id": "p1",
                "api_key_id": 7,
                "scopes": scopes,
                "source": "api",
                "roles": ["member", *SERVICE_CAPABILITIES],
                "is_system_admin": False,
            }

        monkeypatch.setitem(app.dependency_overrides, get_principal, principal)

    install()
    return install


@pytest.fixture
def provider_calls(monkeypatch):
    """Synthetic provider transports plus credit settlement, recording the frozen route."""
    calls: dict[str, list] = {"messages": [], "responses": [], "billed": []}

    async def messages(**kwargs):
        calls["messages"].append(kwargs)
        return {
            "id": "msg_1", "type": "message", "role": "assistant", "model": kwargs["model"],
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }

    async def responses(**kwargs):
        calls["responses"].append(kwargs)
        return {"id": "resp_1", "object": "response", "model": kwargs["model"], "output": [],
                "usage": {"input_tokens": 3, "output_tokens": 2}}

    async def bill(resolved, *_args, **_kwargs):
        calls["billed"].append(resolved)
        return 3, 2, Decimal("0")

    async def precheck(*_args, **_kwargs):
        return None

    monkeypatch.setattr(litellm_client, "aanthropic_messages", messages)
    monkeypatch.setattr(litellm_client, "aresponses", responses)
    monkeypatch.setattr(core, "_bill", bill)
    monkeypatch.setattr(core, "precheck", precheck)
    return calls


def _messages_body(model: str) -> dict:
    return {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}


class TestCliCatalog:
    async def test_catalog_lists_active_text_routes_by_provider_with_honest_eligibility(
        self, client, catalog, api_key_principal
    ):
        response = await client.get("/v1/cli/models", headers=_H)

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        rows = response.json()["models"]
        assert [row["id"] for row in rows] == [
            "lumen/2/21", "lumen/1/11", "lumen/1/12", "lumen/1/15", "lumen/1/16", "lumen/1/17", "lumen/1/18",
            "lumen/1/19", "lumen/1/20", "lumen/3/31", "lumen/4/41", "lumen/5/51",
        ]
        by_id = {row["id"]: row for row in rows}
        gateway, direct = by_id["lumen/2/21"], by_id["lumen/1/11"]
        assert gateway["api_model_name"] == direct["api_model_name"] == "shared-coder"
        assert (gateway["provider"], gateway["provider_name"], gateway["provider_type"]) == (
            "nvidia", "NVIDIA Gateway", "openai",
        )
        assert gateway["display_name"] == "Shared Coder (gateway)"
        assert direct["display_name"] == "shared-coder"
        for row in (gateway, direct):
            assert row["usable"] is True and row["disabled_reason"] is None
        assert gateway["protocols"] == ["messages"]
        assert direct["protocols"] == ["messages", "responses"]
        assert (gateway["input_price_per_million"], gateway["output_price_per_million"]) == ("0.5", "2")
        assert (direct["input_price_per_million"], direct["output_price_per_million"]) == ("1.25", "10")

        assert (by_id["lumen/3/31"]["usable"], by_id["lumen/3/31"]["protocols"]) == (True, ["messages"])
        for route_id, reason in (
            ("lumen/1/12", "pricing_unavailable"),
            ("lumen/1/15", "tools_disabled"),
            ("lumen/4/41", "subscription_protocol_unsupported"),
            ("lumen/5/51", "provider_credentials_missing"),
        ):
            row = by_id[route_id]
            assert (row["usable"], row["protocols"], row["disabled_reason"]) == (False, [], reason)
        assert by_id["lumen/1/12"]["input_price_per_million"] is None
        assert by_id["lumen/1/15"]["capabilities"]["function_calling"] is False

        assert all(row["capabilities"].get("streaming") is True for row in rows if row["usable"])
        assert all("feature_gates" not in row["capabilities"] for row in rows)
        for secret in (*_SECRETS, "gateway.invalid"):
            assert secret not in response.text

    @pytest.mark.parametrize("api_base, protocols", [
        (None, ["messages", "responses"]),
        ("https://api.openai.com", ["messages", "responses"]),
        ("https://api.openai.com/v1/", ["messages", "responses"]),
        ("https://chat-only.invalid/v1", ["messages"]),
    ])
    async def test_catalog_does_not_advertise_native_responses_for_custom_openai_base(
        self, client, catalog, api_key_principal, api_base, protocols
    ):
        with catalog.begin() as session:
            session.get(LlmProvider, 2).api_base = api_base

        response = await client.get("/v1/cli/models", headers=_H)

        assert response.status_code == 200
        row = next(item for item in response.json()["models"] if item["id"] == "lumen/2/21")
        assert row["usable"] is True and row["disabled_reason"] is None
        assert row["protocols"] == protocols
        assert (row["input_price_per_million"], row["output_price_per_million"]) == ("0.5", "2")
        assert "api_base" not in row

    async def test_catalog_disables_nonstreaming_and_explicit_nontext_modalities(
        self, client, catalog, api_key_principal
    ):
        response = await client.get("/v1/cli/models", headers=_H)

        assert response.status_code == 200
        by_id = {row["id"]: row for row in response.json()["models"]}
        for route_id in ("lumen/1/16", "lumen/1/17", "lumen/1/19"):
            row = by_id[route_id]
            assert (row["usable"], row["protocols"], row["disabled_reason"]) == (False, [], "text_unavailable")
        # The consumer still sees the authoritative metadata, not invented streaming or output support.
        assert by_id["lumen/1/16"]["capabilities"]["streaming"] is False
        assert by_id["lumen/1/17"]["capabilities"]["output_modalities"] == ["image"]
        assert by_id["lumen/1/19"]["capabilities"]["input_modalities"] == ["image"]

    async def test_catalog_preserves_unknown_modalities_and_non_authoritative_tool_probes(
        self, client, catalog, api_key_principal
    ):
        response = await client.get("/v1/cli/models", headers=_H)

        assert response.status_code == 200
        by_id = {row["id"]: row for row in response.json()["models"]}
        unknown = by_id["lumen/1/18"]
        assert unknown["capabilities"]["input_modalities"] == []
        assert unknown["capabilities"]["output_modalities"] == []
        assert (unknown["usable"], unknown["protocols"], unknown["disabled_reason"]) == (
            True, ["messages", "responses"], None,
        )
        unknown_null = by_id["lumen/1/20"]
        assert unknown_null["capabilities"]["input_modalities"] is None
        assert unknown_null["capabilities"]["output_modalities"] is None
        assert (unknown_null["usable"], unknown_null["protocols"], unknown_null["disabled_reason"]) == (
            True, ["messages", "responses"], None,
        )
        # These deliberately uncatalogued model names have negative tool probes, not stored tool disables.
        for route_id in ("lumen/1/11", "lumen/1/18"):
            row = by_id[route_id]
            assert row["capabilities"]["function_calling"] is False
            assert row["usable"] is True

    async def test_catalog_never_reads_credentials(self, client, catalog, api_key_principal, monkeypatch):
        def forbidden(_provider):
            raise AssertionError("catalog must not resolve provider credentials")

        monkeypatch.setattr(routing, "resolve_api_key", forbidden)

        assert (await client.get("/v1/cli/models", headers=_H)).status_code == 200

    async def test_catalog_requires_api_key_with_models_read(self, client, catalog, api_key_principal):
        api_key_principal(scopes=(), auth_type="keystone")
        keystone = await client.get("/v1/cli/models")
        api_key_principal(scopes=("compat:completions:write",))
        unscoped = await client.get("/v1/cli/models", headers=_H)

        assert keystone.status_code == 401
        assert unscoped.status_code == 403
        assert "models" not in keystone.json() and "models" not in unscoped.json()

    def test_openapi_publishes_api_key_only_catalog_contract(self):
        operation = app.openapi()["paths"]["/v1/cli/models"]["get"]

        assert operation["security"] == [{"APIKeyBearer": []}, {"XApiKey": []}]
        assert operation["x-required-api-key-scopes"] == ["models:read"]
        assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/CliModelListResponse"
        }

    async def test_catalog_is_hidden_outside_configured_api_hosts(
        self, client, catalog, api_key_principal, monkeypatch
    ):
        monkeypatch.setattr("lumen.auth.get_settings", lambda: SimpleNamespace(chat_api_hosts="api.cloud.example"))

        assert (await client.get("/v1/cli/models", headers=_H)).status_code == 404
        allowed = await client.get("/v1/cli/models", headers={**_H, "host": "api.cloud.example"})
        assert allowed.status_code == 200

    async def test_catalog_storage_failure_is_not_an_empty_success(self, client, api_key_principal, monkeypatch):
        def unavailable():
            raise ChatStorageUnavailable("down")

        monkeypatch.setattr(routing, "_require_db", unavailable)
        down = await client.get("/v1/cli/models", headers=_H)

        class Broken(_Session):
            async def execute(self, statement):
                raise OperationalError("SELECT", {}, Exception("connection lost"))

        marked = []
        monkeypatch.setattr(routing, "_require_db", lambda: lambda: Broken(SimpleNamespace(close=lambda: None)))
        monkeypatch.setattr(routing, "mark_db_unhealthy", lambda: marked.append(True))
        broken = await client.get("/v1/cli/models", headers=_H)

        for response in (down, broken):
            assert response.status_code == 503
            assert response.headers["cache-control"] == "no-store"
            assert "models" not in response.json()
        assert marked == [True]


class TestExactRouteResolution:
    async def test_route_id_selects_exactly_one_active_pair_and_keeps_every_selector(self, catalog):
        gateway = await routing.resolve_api_model("lumen/2/21")

        assert (gateway["provider_id"], gateway["model_id"]) == (2, 21)
        assert gateway["model_name"] == "shared-coder" and gateway["api_key"] == "key-2"
        assert gateway["api_base"] == "https://gateway.invalid/v1"
        assert (await routing.resolve_api_model("lumen/2/21", provider="nvidia", provider_type="openai"))[
            "model_id"
        ] == 21
        for kwargs in (
            {"provider": "openai"},
            {"provider_id": 1},
            {"provider_type": "anthropic"},
            {"model_kind": "image"},
        ):
            assert await routing.resolve_api_model("lumen/2/21", **kwargs) is None
        # Inactive rows, a model on another connection and kind-mismatched IDs never fall back by name.
        for route_id in ("lumen/6/61", "lumen/1/13", "lumen/2/11", "lumen/1/14", "lumen/9/21"):
            assert await routing.resolve_api_model(route_id) is None
        assert (await routing.resolve_api_model("lumen/1/14", model_kind="image"))["model_id"] == 14

    async def test_public_ids_keep_their_ambiguity_contract(self, catalog):
        with pytest.raises(AmbiguousModelRouteError):
            await routing.resolve_api_model("shared-coder")
        assert (await routing.resolve_api_model("shared-coder", provider="nvidia"))["model_id"] == 21
        # Non-canonical tokens are ordinary public IDs, which no route exposes here.
        for value in (
            "lumen/02/21", "lumen/0/21", "lumen/2/21/", "lumen/2", "lumen/2/21\n",
            "lumen/9223372036854775808/21", "lumen/2/9223372036854775808", f"lumen/2/{'9' * 100}",
        ):
            assert await routing.resolve_api_model(value) is None


class TestCompatRouteTargets:
    async def test_messages_and_responses_execute_and_bill_the_selected_duplicate(
        self, client, catalog, api_key_principal, provider_calls
    ):
        messages = await client.post("/v1/messages", json=_messages_body("lumen/2/21"), headers=_H)
        responses = await client.post(
            "/v1/responses", json={"model": "lumen/1/11", "input": "hi"}, headers=_H
        )

        assert messages.status_code == 200, messages.text
        assert responses.status_code == 200, responses.text
        sent_messages, sent_responses = provider_calls["messages"][0], provider_calls["responses"][0]
        assert (sent_messages["model"], sent_messages["api_key"], sent_messages["api_base"]) == (
            "shared-coder", "key-2", "https://gateway.invalid/v1",
        )
        assert (sent_responses["model"], sent_responses["api_key"], sent_responses["api_base"]) == (
            "shared-coder", "key-1", None,
        )
        billed = [(route["provider_id"], route["model_id"], route["model_name"]) for route in provider_calls["billed"]]
        assert billed == [(2, 21, "shared-coder"), (1, 11, "shared-coder")]

    async def test_route_id_conflicts_and_inactive_routes_fail_without_inference(
        self, client, catalog, api_key_principal, provider_calls
    ):
        conflicting = await client.post(
            "/v1/messages", json=_messages_body("lumen/2/21"), headers={**_H, "X-Lumen-Provider": "openai"}
        )
        inactive = await client.post("/v1/responses", json={"model": "lumen/6/61", "input": "hi"}, headers=_H)
        ambiguous = await client.post("/v1/messages", json=_messages_body("shared-coder"), headers=_H)

        assert conflicting.status_code == 404
        assert inactive.status_code == 404
        assert ambiguous.status_code == 409
        assert provider_calls == {"messages": [], "responses": [], "billed": []}

    async def test_unsupported_subscription_protocols_are_client_errors(
        self, client, catalog, api_key_principal, monkeypatch
    ):
        async def precheck(*_args, **_kwargs):
            return None

        monkeypatch.setattr(core, "precheck", precheck)
        claude_on_responses = await client.post(
            "/v1/responses", json={"model": "lumen/3/31", "input": "hi"}, headers=_H
        )
        chatgpt_on_messages = await client.post("/v1/messages", json=_messages_body("lumen/4/41"), headers=_H)

        assert claude_on_responses.status_code == 400
        assert claude_on_responses.json()["error"]["type"] == "invalid_request_error"
        assert chatgpt_on_messages.status_code == 400
        assert chatgpt_on_messages.json()["error"]["type"] == "invalid_request_error"

    async def test_count_tokens_stays_authoritative_for_route_ids(self, client, catalog, api_key_principal):
        response = await client.post(
            "/v1/messages/count_tokens",
            json={"model": "lumen/2/21", "messages": [{"role": "user", "content": "hi"}]},
            headers=_H,
        )

        assert response.status_code == 501
        assert response.json()["error"]["message"] == "token_count_unavailable"
