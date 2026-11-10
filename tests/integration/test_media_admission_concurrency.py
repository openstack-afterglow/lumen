"""Concurrent media admissions on one provider: no deadlock, no lost request, one run per key.

Admission runs READ COMMITTED (no gap locks, which used to deadlock racing admissions with MariaDB 1213 and
surface as HTTP 500). The unique ``uq_chat_runs_idempotency`` index (migration 029) is therefore the sole
authority for a same-key race: the loser's INSERT fails and the admission replays the winner's run. Before
that index existed READ COMMITTED admitted every racing same-key request as its own run.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal

import pytest
from provider_fixtures import configured_provider
from sqlalchemy import delete, event, func, select

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_db import LlmModel, LlmProvider
from lumen.models.chat_runs import ChatRun
from lumen.services import assets
from lumen.services.durable_runs import audio, images, realtime

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("current_project_authority")]

_REQUESTS = 12


@pytest.fixture
def admitters(monkeypatch):
    """Per-kind ``((model, kind, pricing), build(model_id) -> admit(project, user, key) -> run id)``."""
    monkeypatch.setattr(assets, "asset_pipeline_available", lambda: True)

    async def no_wake(_run_id):
        return None

    monkeypatch.setattr(images, "wake_run", no_wake)
    monkeypatch.setattr(audio, "wake_run", no_wake)

    def image(model_id):
        body = {"model_id": str(model_id), "prompt": "A cobalt cube", "size": "1024x1024", "quality": "high", "n": 1}

        async def admit(project_id, user_id, key):
            return (await images.admit_image_run(body, project_id=project_id, user_id=user_id,
                                                 client_request_id=key)).run_id
        return admit

    def tts(model_id):
        body = {"kind": "tts", "model_id": str(model_id), "input": "hello", "voice": "alloy", "response_format": "wav"}

        async def admit(project_id, user_id, key):
            return (await audio.admit_audio_run(body, user_id=user_id, project_id=project_id,
                                                client_request_id=key)).run_id
        return admit

    def realtime_session(model_id):
        body = {"model_id": str(model_id), "max_duration_seconds": 10}

        async def admit(project_id, user_id, key):
            return (await realtime.admit_realtime_session(body, project_id=project_id, user_id=user_id,
                                                          client_request_id=key))["session_id"]
        return admit

    return {
        "image": (("gpt-image-1.5", "image", {"image_variants": {"1024x1024:high": "0.04"}}), image),
        "tts": (("tts-1", "tts", {"audio_per_second": "0.001"}), tts),
        "realtime": (("gpt-realtime", "realtime", {"realtime_input_per_minute": "0.012",
                                                   "realtime_output_per_minute": "0.024"}), realtime_session),
    }


@pytest.mark.parametrize("same_key", [False, True], ids=["distinct-keys", "one-key"])
@pytest.mark.parametrize("kind", ["image", "tts", "realtime"])
@pytest.mark.parametrize("snapshot_isolation", ["OFF", "ON"])
async def test_concurrent_admissions_all_succeed_and_one_key_is_one_run(
        request, admitters, snapshot_isolation, kind, same_key):
    from sqlalchemy.pool import Pool

    def pin_isolation(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET SESSION innodb_snapshot_isolation={snapshot_isolation}")
        cursor.close()

    event.listen(Pool, "connect", pin_isolation)
    request.addfinalizer(lambda: event.remove(Pool, "connect", pin_isolation))
    init_db(os.environ["DATABASE_URL"], pool_size=_REQUESTS + 2, max_overflow=0)
    factory = get_session_factory()
    assert factory is not None
    nonce = uuid.uuid4().hex
    user_id, project_id = f"admit-user-{nonce}", f"admit-project-{nonce}"
    (model_name, model_kind, pricing), build = admitters[kind]
    provider_id = None
    try:
        async with factory() as session, session.begin():
            provider = configured_provider(name=f"admit-provider-{nonce}", provider_type="openai",
                                           is_active=True, margin_multiplier=Decimal("1"))
            session.add(provider)
            await session.flush()
            provider_id = provider.id
            model = LlmModel(provider_id=provider_id, model_name=model_name, model_kind=model_kind,
                             media_pricing=pricing, is_active=True)
            session.add(model)
            await session.flush()
            model_id = model.id
        admit = build(model_id)
        shared_key = str(uuid.uuid4())
        run_ids = await asyncio.gather(*(
            admit(project_id, user_id, shared_key if same_key else str(uuid.uuid4())) for _ in range(_REQUESTS)))
        async with factory() as session:
            rows = await session.scalar(select(func.count()).select_from(ChatRun).where(
                ChatRun.project_id == project_id))
        expected = 1 if same_key else _REQUESTS
        assert rows == expected, f"{rows} runs exist for {_REQUESTS} admits ({'one key' if same_key else 'distinct keys'})"
        assert len(set(run_ids)) == expected, f"{len(set(run_ids))} distinct runs returned"
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(ChatRun).where(ChatRun.project_id == project_id))
            if provider_id is not None:
                await session.execute(delete(LlmModel).where(LlmModel.provider_id == provider_id))
                await session.execute(delete(LlmProvider).where(LlmProvider.id == provider_id))
        await close_db()
