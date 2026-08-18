"""Real MariaDB/Redis coverage for the API-key durable-run path."""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from lumen.db import close_db, get_session_factory, init_db
from lumen.main import app
from lumen.models.chat_db import LlmModel, LlmProvider
from lumen.services import api_key_store, graph
from lumen.services.durable_runs import execution

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

            admitted = await client.post(
                "/v1/temp-completions",
                headers={"X-Api-Key": key["key"], "Idempotency-Key": str(uuid.uuid4())},
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
                        choices=[SimpleNamespace(delta=SimpleNamespace(content="integration complete", tool_calls=None))],
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
                line.removeprefix("event: ")
                for line in events.text.splitlines()
                if line.startswith("event: ")
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
    finally:
        await close_db()
