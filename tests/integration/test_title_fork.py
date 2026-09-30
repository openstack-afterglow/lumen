"""Real MariaDB coverage for a fork's first title and late manual-rename fence."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_db import ChatUsageLog
from lumen.models.chat_jobs import ChatJob
from lumen.models.chat_runs import ChatRun
from lumen.services import conversation_store as conversations
from lumen.services import title_jobs
from lumen.services.title_summary import TitleResult

pytestmark = pytest.mark.integration


async def test_inherited_first_user_enqueues_title_without_overwriting_manual_rename() -> None:
    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    nonce = uuid.uuid4().hex
    owner = {"user_id": f"title-user-{nonce}", "project_id": f"title-project-{nonce}"}
    try:
        source = await conversations.create_conversation(title=None, model_name="title-test-model", **owner)
        user = await conversations.add_message(
            source["id"], role="user", content="첫 상속 질문", set_leaf=True,
        )
        fork = await conversations.fork_conversation(source["id"], message_id=user["id"], **owner)
        assistant = await conversations.add_message(
            fork["id"], role="assistant", content="분기에서 받은 답변", parent_id=user["id"], set_leaf=True,
        )
        factory = get_session_factory()
        assert factory is not None
        run_id = str(uuid.uuid4())
        async with factory() as session, session.begin():
            run = ChatRun(
                id=run_id,
                run_scope="persistent",
                run_kind="completion",
                conversation_id=fork["id"],
                user_message_id=user["id"],
                assistant_message_id=assistant["id"],
                **owner,
                model_name="title-test-model",
                capability_snapshot={"summary_route": {"model_name": "fake-title-model"}},
                pricing_snapshot={"summary_route": {
                    "provider_name": "fake", "price_source": "manual",
                    "input_price_per_token": "0", "output_price_per_token": "0",
                }},
                client_request_id=str(uuid.uuid4()),
                request_fingerprint=nonce,
                fingerprint_version=1,
                status="completed",
            )
            session.add(run)
            await session.flush()
            assert await title_jobs.enqueue_completed_run_in_transaction(session, run)
            assert not await title_jobs.enqueue_completed_run_in_transaction(session, run)
            await session.flush()
            job = await session.scalar(select(ChatJob).where(ChatJob.conversation_id == fork["id"]))
            assert job is not None
            job_id = job.id
            assert job.payload is not None
            assert "첫 상속 질문" not in job.payload
            assert title_jobs._json_load(job.payload)["exchange"] == [
                {"role": "user", "content": "첫 상속 질문"},
                {"role": "assistant", "content": "분기에서 받은 답변"},
            ]

        pending = await conversations.get_conversation(fork["id"], **owner)
        untouched_source = await conversations.get_conversation(source["id"], **owner)
        assert (pending["title"], pending["title_status"], pending["title_revision"]) == (None, "pending", 1)
        assert (untouched_source["title"], untouched_source["title_status"]) == (None, "idle")
        renamed = await conversations.update_title(fork["id"], title="직접 지정", **owner)
        assert (renamed["title"], renamed["title_source"], renamed["title_revision"]) == (
            "직접 지정", "explicit", 2,
        )

        lease_owner = f"title-worker-{nonce}"
        async with factory() as session, session.begin():
            job = await session.get(ChatJob, job_id, with_for_update=True)
            assert job is not None
            job.status = "running"
            job.lease_owner = lease_owner
            job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
        claimed = {"job_id": job_id, "owner": lease_owner}
        await title_jobs._store_result(
            claimed,
            TitleResult(
                title="늦게 도착한 자동 제목", prompt_tokens=2, completion_tokens=3,
                messages=[], model_name="fake-title-model",
            ),
        )
        assert await title_jobs._apply_result(claimed)
        assert not await title_jobs._apply_result(claimed)

        public = await conversations.get_conversation(fork["id"], **owner)
        assert (public["title"], public["title_source"], public["title_status"], public["title_revision"]) == (
            "직접 지정", "explicit", "ready", 2,
        )
        async with factory() as session:
            ledger = (await session.execute(
                select(ChatUsageLog).where(ChatUsageLog.event_id == f"title:{fork['id']}:1")
            )).scalars().all()
            assert len(ledger) == 1
            assert ledger[0].source == "system"
            assert ledger[0].run_id == run_id
        await conversations.delete_conversation(fork["id"], **owner)
        await conversations.delete_conversation(source["id"], **owner)
    finally:
        await close_db()
