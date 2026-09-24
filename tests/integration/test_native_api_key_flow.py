"""Real MariaDB/Redis coverage for the API-key durable-run path."""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import delete, func, select

from lumen.db import close_db, get_session_factory, init_db
from lumen.main import app
from lumen.models.chat_db import LlmModel, LlmProvider
from lumen.models.chat_runs import ChatRun
from lumen.services import api_key_store, graph
from lumen.services.durable_runs import execution
from lumen.services.providers import repository
from lumen.services.providers.errors import ProviderValidationError

pytestmark = pytest.mark.integration


async def test_scoped_api_key_admits_executes_and_replays_a_native_run(monkeypatch):
    """Exercise HTTP admission, the durable journal, worker execution, and usage attribution."""
    database_url = os.environ["DATABASE_URL"]
    redis_url = os.environ["REDIS_URL"]
    nonce = uuid.uuid4().hex
    user_id = f"integration-user-{nonce}"
    project_id = f"integration-project-{nonce}"
    model_name = f"integration-model-{nonce}"
    run_owner = f"integration-worker-{nonce}"

    init_db(database_url, pool_size=1, max_overflow=0)
    factory = get_session_factory()
    assert factory is not None

    try:
        async with factory() as session, session.begin():
            provider = LlmProvider(
                name=f"integration-provider-{nonce}",
                provider_type="openai",
                is_active=True,
                margin_multiplier=Decimal("1"),
            )
            session.add(provider)
            await session.flush()
            session.add(
                LlmModel(
                    provider_id=provider.id,
                    model_name=model_name,
                    is_active=True,
                    input_price=Decimal("0.000001"),
                    output_price=Decimal("0.000002"),
                    price_source="manual",
                )
            )

        redis = Redis.from_url(redis_url)
        try:
            assert await redis.ping() is True
        finally:
            await redis.close()

        key = await api_key_store.create_key(
            user_id,
            project_id,
            "native integration",
            ["models:read", "native:runs:read", "native:runs:write", "native:tools:execute", "usage:read"],
            None,
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            unknown = await client.post(
                "/v1/temp-completions",
                headers={"X-Api-Key": "sk-afgl-integration-invalid", "Idempotency-Key": str(uuid.uuid4())},
                json={
                    "parts": [{"type": "text", "text": "hello"}],
                    "model_id": model_name,
                    "features": {"memory": False, "tool_policy": {"mode": "none"}},
                },
            )
            assert unknown.status_code == 401
            assert unknown.json()["detail"] == "유효하지 않은 API 키입니다"

            first_idem_key = str(uuid.uuid4())
            admitted = await client.post(
                "/v1/temp-completions",
                headers={"X-Api-Key": key["key"], "Idempotency-Key": first_idem_key},
                json={
                    "parts": [{"type": "text", "text": "run the integration check"}],
                    "model_id": model_name,
                    "features": {"memory": False, "tool_policy": {"mode": "agent_default"}},
                },
            )
            assert admitted.status_code == 202, admitted.text
            descriptor = admitted.json()
            run_id = descriptor["run_id"]
            assert descriptor["status"] == "queued"

            responses = [
                [
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    content=None,
                                    tool_calls=[
                                        SimpleNamespace(
                                            index=0,
                                            id="call-1",
                                            function=SimpleNamespace(
                                                name="list_my_conversations",
                                                arguments="{}",
                                            ),
                                        )
                                    ],
                                )
                            )
                        ],
                        usage=None,
                    )
                ],
                [
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(delta=SimpleNamespace(content="integration complete", tool_calls=None))
                        ],
                        usage=None,
                    ),
                    SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=None))],
                        usage={"prompt_tokens": 3, "completion_tokens": 2},
                    ),
                ],
            ]
            provider_call = 0

            async def fake_litellm_stream(**kwargs):
                nonlocal provider_call
                assert kwargs["model"] == model_name
                chunks = responses[provider_call]
                provider_call += 1

                async def stream():
                    for chunk in chunks:
                        yield chunk

                return stream()

            monkeypatch.setattr(graph.litellm_client, "acompletion_stream", fake_litellm_stream)
            assert await execution.execute_queued_run(run_id, owner=run_owner) is True

            events = await client.get(f"/v1/runs/{run_id}/events", headers={"X-Api-Key": key["key"]})
            assert events.status_code == 200, events.text
            event_types = [
                line.removeprefix("event: ") for line in events.text.splitlines() if line.startswith("event: ")
            ]
            assert {"tool.call.started", "tool.call.completed", "usage.updated", "run.completed"} <= set(event_types)
            event_ids = [line.removeprefix("id: ") for line in events.text.splitlines() if line.startswith("id: ")]
            assert event_ids == [f"{run_id}:{index}" for index in range(1, len(event_ids) + 1)]

            usage = await client.get("/v1/usage/records", headers={"X-Api-Key": key["key"]})
            assert usage.status_code == 200, usage.text
            record = next(item for item in usage.json()["records"] if item["run_id"] == run_id)
            assert record["source"] == "api"
            assert record["api_key_id"] == key["id"]
            assert {"raw_cost", "pricing_snapshot", "usage_components"}.isdisjoint(record)

            owner_project_id = project_id

            def fake_validate_token(token: str, project_id: str = "") -> dict:
                assert token == "integration-owner-token"
                return {
                    "user_id": user_id,
                    "username": "integration-user",
                    "project_id": project_id or owner_project_id,
                    "roles": ["member"],
                    "is_system_admin": False,
                }

            monkeypatch.setattr("lumen.auth.validate_token", fake_validate_token)

            owner_headers = {"X-Auth-Token": "integration-owner-token"}
            api_keys_res = await client.get("/v1/api-keys", headers=owner_headers)
            assert api_keys_res.status_code == 200, api_keys_res.text
            found_key = next(item for item in api_keys_res.json() if item["id"] == key["id"])
            assert found_key["month_credited_cost"] == record["credited_cost"]

            patch_res = await client.patch(
                f"/v1/api-keys/{key['id']}/limits",
                headers=owner_headers,
                json={"monthly_credit_limit": record["credited_cost"]},
            )
            assert patch_res.status_code == 200, patch_res.text
            updated_key = patch_res.json()
            assert updated_key["owner_monthly_credit_limit"] == record["credited_cost"]
            assert updated_key["effective_monthly_credit_limit"] == record["credited_cost"]

            async with factory() as session:
                runs_before = (await session.execute(select(func.count(ChatRun.id)))).scalar_one()

            provider_calls_before = provider_call

            blocked = await client.post(
                "/v1/temp-completions",
                headers={"X-Api-Key": key["key"], "Idempotency-Key": str(uuid.uuid4())},
                json={
                    "parts": [{"type": "text", "text": "should be blocked by limit"}],
                    "model_id": model_name,
                    "features": {"memory": False, "tool_policy": {"mode": "agent_default"}},
                },
            )
            assert blocked.status_code == 402, blocked.text
            assert blocked.json()["detail"] == "API 키 월 사용 한도를 초과했습니다"

            async with factory() as session:
                runs_after = (await session.execute(select(func.count(ChatRun.id)))).scalar_one()
            assert runs_after == runs_before
            assert provider_call == provider_calls_before

            replayed = await client.post(
                "/v1/temp-completions",
                headers={"X-Api-Key": key["key"], "Idempotency-Key": first_idem_key},
                json={
                    "parts": [{"type": "text", "text": "run the integration check"}],
                    "model_id": model_name,
                    "features": {"memory": False, "tool_policy": {"mode": "agent_default"}},
                },
            )
            assert replayed.status_code == 202, replayed.text
            assert replayed.json()["run_id"] == run_id
    finally:
        await close_db()


async def test_title_recovery_skips_empty_and_unavailable_conversations_without_duplicate_jobs():
    from datetime import UTC, datetime, timedelta

    from lumen.crypto import encrypt_chat_content
    from lumen.models.chat_db import ChatConversation, ChatMessage
    from lumen.models.chat_jobs import ChatJob
    from lumen.services import title_jobs

    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    factory = get_session_factory()
    nonce = uuid.uuid4().hex
    owner = {"user_id": f"recovery-{nonce}", "project_id": f"recovery-{nonce}"}
    empty_ids = [str(uuid.uuid4()) for _ in range(21)]
    unavailable_id, recoverable_id = str(uuid.uuid4()), str(uuid.uuid4())
    now = datetime.now(UTC)
    try:
        async with factory() as session, session.begin():
            session.add_all(
                [
                    ChatConversation(id=conv_id, **owner, title_source="auto", created_at=now - timedelta(days=1))
                    for conv_id in empty_ids
                ]
            )
            for offset, conv_id in enumerate((unavailable_id, recoverable_id)):
                session.add(
                    ChatConversation(
                        id=conv_id, **owner, title_source="auto", created_at=now - timedelta(minutes=20 - offset)
                    )
                )
                await session.flush()
                user = ChatMessage(
                    conversation_id=conv_id, role="user", content=encrypt_chat_content("Plan OpenStack HA")
                )
                assistant = ChatMessage(
                    conversation_id=conv_id, role="assistant", content=encrypt_chat_content("Use three controllers")
                )
                session.add_all([user, assistant])
                await session.flush()
                session.add(
                    ChatRun(
                        id=str(uuid.uuid4()),
                        **owner,
                        conversation_id=conv_id,
                        run_scope="persistent",
                        status="completed",
                        model_name="title-model",
                        user_message_id=user.id,
                        assistant_message_id=assistant.id,
                        capability_snapshot={"summary_route": {"model_name": "title-model", "provider_type": "openai"}}
                        if offset
                        else {},
                        pricing_snapshot={},
                        client_request_id=str(uuid.uuid4()),
                        request_fingerprint=nonce + str(offset),
                        fingerprint_version=1,
                    )
                )

        assert await title_jobs._recover_one() is True
        assert await title_jobs._recover_one() is True
        await title_jobs._recover_one()
        async with factory() as session:
            unavailable = await session.get(ChatConversation, unavailable_id)
            recovered = await session.get(ChatConversation, recoverable_id)
            assert unavailable.title_status == "unavailable"
            assert recovered.title_status == "pending"
            assert recovered.title_revision == 1
            jobs = (
                (await session.execute(select(ChatJob).where(ChatJob.conversation_id == recoverable_id)))
                .scalars()
                .all()
            )
            assert len(jobs) == 1
            assert jobs[0].status == "queued"
            empty_statuses = (
                (await session.execute(select(ChatConversation.title_status).where(ChatConversation.id.in_(empty_ids))))
                .scalars()
                .all()
            )
            assert set(empty_statuses) == {"idle"}
    finally:
        await close_db()


async def test_subscription_model_namespace_is_unique_under_concurrent_registration():
    database_url = os.environ["DATABASE_URL"]
    nonce = uuid.uuid4().hex
    provider_ids: list[int] = []

    init_db(database_url, pool_size=2, max_overflow=0)
    factory = get_session_factory()
    assert factory is not None

    try:
        async with factory() as session, session.begin():
            providers = [
                LlmProvider(
                    name=f"subscription-provider-{index}-{nonce}",
                    provider_type="chatgpt",
                    auth_mode="chatgpt_device",
                    is_active=True,
                    margin_multiplier=Decimal("1"),
                )
                for index in range(2)
            ]
            session.add_all(providers)
            await session.flush()
            provider_ids = [provider.id for provider in providers]

        results = await asyncio.gather(
            *(
                repository.create_model(
                    provider_id=provider_id,
                    model_name=f"gpt-concurrency-{nonce}",
                    input_price_per_million="0",
                    output_price_per_million="0",
                )
                for provider_id in provider_ids
            ),
            return_exceptions=True,
        )

        created = [result for result in results if isinstance(result, dict)]
        rejected = [result for result in results if isinstance(result, ProviderValidationError)]
        assert len(created) == 1
        assert len(rejected) == 1, [
            f"{type(result).__name__}: {result}; cause={getattr(result, '__cause__', None)!r}" for result in results
        ]
        assert created[0]["model_name"] == f"chatgpt/gpt-concurrency-{nonce}"
    finally:
        if provider_ids:
            async with factory() as session, session.begin():
                await session.execute(delete(LlmProvider).where(LlmProvider.id.in_(provider_ids)))
        await close_db()


async def test_second_page_claude_requires_explicit_pricing_before_native_admission(monkeypatch):
    """Discovery is read-only; explicit registration becomes routable and billable without a flush."""
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from urllib.parse import parse_qs, urlsplit

    from lumen.models.chat_db import ChatUsageLog
    from lumen.models.chat_runs import ChatRunProvider

    nonce = uuid.uuid4().hex
    model_name = f"claude-onboarding-unlisted-{nonce}"
    unpriced_name = f"claude-unpriced-{nonce}"
    project_id = f"onboarding-project-{nonce}"
    user_id = f"onboarding-user-{nonce}"
    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    factory = get_session_factory()
    assert factory is not None
    redis = Redis.from_url(os.environ["REDIS_URL"])
    fake_provider = None
    fake_thread = None
    try:
        assert await redis.ping() is True

        def validate_admin(token: str, project_id: str = "") -> dict:
            assert token == "onboarding-admin-token"
            return {
                "user_id": user_id,
                "username": "onboarding-admin",
                "project_id": project_id or f"onboarding-project-{nonce}",
                "roles": ["admin"],
                "is_system_admin": True,
            }

        monkeypatch.setattr("lumen.auth.validate_token", validate_admin)
        admin_headers = {"X-Auth-Token": "onboarding-admin-token"}
        requested_pages: list[str | None] = []

        class FakeClaudeCatalog(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                cursor = query.get("after_id", [None])[0]
                if (
                    parsed.path != "/v1/models"
                    or self.headers.get("x-api-key") != "isolated-fake-key"
                    or self.headers.get("anthropic-version") != "2023-06-01"
                    or query.get("limit") != ["200"]
                    or cursor not in (None, "claude-sonnet-4-6")
                ):
                    self.send_error(400)
                    return
                requested_pages.append(cursor)
                mid, display_name = (
                    ("claude-sonnet-4-6", "Sonnet") if cursor is None else (model_name, "Unlisted Claude")
                )
                body = json.dumps(
                    {
                        "data": [
                            {"id": mid, "display_name": display_name, "type": "model"},
                            *(
                                [{"id": unpriced_name, "display_name": "Unpriced Claude", "type": "model"}]
                                if cursor
                                else []
                            ),
                        ],
                        "has_more": cursor is None,
                        "last_id": unpriced_name if cursor else mid,
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        fake_provider = ThreadingHTTPServer(("127.0.0.1", 0), FakeClaudeCatalog)
        fake_thread = Thread(target=fake_provider.serve_forever, daemon=True)
        fake_thread.start()
        key = await api_key_store.create_key(
            user_id,
            project_id,
            "onboarding",
            ["models:read", "native:runs:read", "native:runs:write", "native:tools:execute", "usage:read"],
            None,
        )
        key_headers = {"X-Api-Key": key["key"]}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            created_provider = await client.post(
                "/v1/admin/providers",
                headers=admin_headers,
                json={
                    "name": f"onboarding-provider-{nonce}",
                    "provider_type": "anthropic",
                    "api_base": f"http://127.0.0.1:{fake_provider.server_port}/v1",
                    "api_key": "isolated-fake-key",
                },
            )
            assert created_provider.status_code == 201, created_provider.text
            provider_id = created_provider.json()["id"]

            discovered = await client.get(f"/v1/admin/providers/{provider_id}/available-models", headers=admin_headers)
            assert discovered.status_code == 200, discovered.text
            catalog = discovered.json()
            assert catalog["provider_id"] == provider_id
            assert catalog["source"] == "api" and catalog["live_status"] == "success"
            assert catalog["complete"] is True and catalog["error"] is None
            assert catalog["models"] == ["claude-sonnet-4-6", model_name, unpriced_name]
            candidate = next(item for item in catalog["candidates"] if item["id"] == model_name)
            assert candidate["display_name"] == "Unlisted Claude" and candidate["purpose"] == "chat"
            assert requested_pages == [None, "claude-sonnet-4-6"]
            async with factory() as session:
                count = await session.scalar(select(func.count(LlmModel.id)).where(LlmModel.provider_id == provider_id))
            assert count == 0, "Discovery must not persist a model"

            unpriced = await client.post(
                "/v1/admin/models",
                headers=admin_headers,
                json={"provider_id": provider_id, "model_name": unpriced_name},
            )
            assert unpriced.status_code == 201, unpriced.text
            registered = await client.post(
                "/v1/admin/models",
                headers=admin_headers,
                json={
                    "provider_id": provider_id,
                    "model_name": model_name,
                    "input_price_per_million": "2",
                    "output_price_per_million": "4",
                },
            )
            assert registered.status_code == 201, registered.text
            model = registered.json()
            assert model["provider_id"] == provider_id
            assert model["api_model_name"] == model_name and model["api_provider"] == "anthropic"
            assert model["price_source"] == "manual"
            assert model["effective_capabilities"]["feature_gates"]["text"]["pricing_available"] is True

            active = await client.get("/v1/admin/models?active_only=true", headers=admin_headers)
            assert active.status_code == 200, active.text
            assert any(item["id"] == model["id"] for item in active.json())
            unpriced_projection = next(item for item in active.json() if item["id"] == unpriced.json()["id"])
            assert unpriced_projection["effective_price_source"] == "unpriced", unpriced_projection
            assert unpriced_projection["effective_input_price_per_million"] is None
            assert unpriced_projection["effective_output_price_per_million"] is None
            public = await client.get("/v1/models", headers=key_headers)
            assert public.status_code == 200, public.text
            assert "anthropic" in next(item for item in public.json()["data"] if item["id"] == model_name)["providers"]

            def native_request(selected_model: str, text: str, *, search: bool = False) -> dict:
                return {
                    "parts": [{"type": "text", "text": text}],
                    "model_id": selected_model,
                    "features": {
                        "memory": False,
                        "tool_policy": {"mode": "agent_default" if search else "none"},
                        **({"web_search": {"enabled": True, "mode": "native"}} if search else {}),
                    },
                }

            async with factory() as session:
                before_denial = await session.scalar(select(func.count(ChatRun.id)).where(ChatRun.user_id == user_id))
            denied = await client.post(
                "/v1/temp-completions",
                headers={**key_headers, "Idempotency-Key": str(uuid.uuid4())},
                json=native_request(unpriced_name, "no price must deny"),
            )
            assert denied.status_code == 422, denied.text
            assert "text (pricing_unavailable)" in denied.json()["detail"]

            advanced = await client.post(
                "/v1/temp-completions",
                headers={**key_headers, "Idempotency-Key": str(uuid.uuid4())},
                json=native_request(model_name, "unsupported search", search=True),
            )
            async with factory() as session:
                after_denial = await session.scalar(select(func.count(ChatRun.id)).where(ChatRun.user_id == user_id))
            assert before_denial == after_denial == 0
            assert advanced.status_code == 422, advanced.text
            assert "web_search" in advanced.json()["detail"]

            admitted = await client.post(
                "/v1/temp-completions",
                headers={**key_headers, "Idempotency-Key": str(uuid.uuid4())},
                json=native_request(model_name, "priced text only"),
            )
            assert admitted.status_code == 202, admitted.text
            run_id = admitted.json()["run_id"]
            async with factory() as session:
                run = await session.get(ChatRun, run_id)
                route = await session.get(ChatRunProvider, (run_id, "executor"))
                assert run is not None and route is not None
                assert route.provider_id == provider_id and route.model_id == model["id"]
                assert run.capability_snapshot["config_version_hash"] == route.config_version_hash
                assert run.pricing_snapshot["price_source"] == "manual"
                assert Decimal(run.pricing_snapshot["input_price_per_token"]) == Decimal("0.000002")
                assert Decimal(run.pricing_snapshot["output_price_per_token"]) == Decimal("0.000004")
                admitted_pricing = dict(run.pricing_snapshot)

            async def fake_stream(**kwargs):
                assert kwargs["model"] == model_name
                assert kwargs["custom_llm_provider"] == "anthropic"

                async def chunks():
                    yield SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content="fake Claude answer", tool_calls=None))],
                        usage=None,
                    )
                    yield SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=None))],
                        usage={"prompt_tokens": 10, "completion_tokens": 5},
                    )

                return chunks()

            monkeypatch.setattr(graph.litellm_client, "acompletion_stream", fake_stream)
            assert await execution.execute_queued_run(run_id, owner=f"onboarding-worker-{nonce}") is True
            async with factory() as session:
                stored = await session.get(ChatRun, run_id)
                ledger = (await session.execute(select(ChatUsageLog).where(ChatUsageLog.run_id == run_id))).scalar_one()
                assert stored.status == "completed"
                assert ledger.provider == f"onboarding-provider-{nonce}"
                assert (ledger.prompt_tokens, ledger.completion_tokens) == (10, 5)
                assert ledger.raw_cost == Decimal("0.0000400000")
                assert ledger.pricing_status == "priced"
            usage = await client.get("/v1/usage/records", headers=key_headers)
            assert usage.status_code == 200, usage.text
            record = next(item for item in usage.json()["records"] if item["run_id"] == run_id)
            assert record["source"] == "api" and record["api_key_id"] == key["id"]
            assert record["total_tokens"] == 15

            repriced = await client.patch(
                f"/v1/admin/models/{model['id']}",
                headers=admin_headers,
                json={"input_price_per_million": "6", "output_price_per_million": "8"},
            )
            assert repriced.status_code == 200, repriced.text
            async with factory() as session:
                unchanged_run = await session.get(ChatRun, run_id)
                unchanged_ledger = (
                    await session.execute(select(ChatUsageLog).where(ChatUsageLog.run_id == run_id))
                ).scalar_one()
                assert unchanged_run.pricing_snapshot == admitted_pricing
                assert unchanged_ledger.raw_cost == Decimal("0.0000400000")
    finally:
        if fake_provider is not None:
            fake_provider.shutdown()
            fake_provider.server_close()
        if fake_thread is not None:
            fake_thread.join()
        await redis.close()
        await close_db()
