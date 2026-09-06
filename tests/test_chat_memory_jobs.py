"""Durable post-response memory extraction job tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lumen.services import memory_jobs as jobs
from lumen.services import title_jobs

pytestmark = pytest.mark.asyncio


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Session:
    def __init__(self, existing_job_id=None):
        self.existing_job_id = existing_job_id
        self.added = []

    async def execute(self, _statement):
        return _Result(self.existing_job_id)

    def add(self, value):
        self.added.append(value)


def _run(**overrides):
    values = {
        "id": "run-1",
        "status": "completed",
        "run_scope": "persistent",
        "parent_run_id": None,
        "conversation_id": "conversation-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def test_enqueue_completed_run_uses_one_idempotency_key():
    session = _Session()

    await jobs.enqueue_completed_run_in_transaction(session, _run(), memory_enabled=True)

    assert len(session.added) == 1
    assert session.added[0].kind == "memory_extract"
    assert session.added[0].idempotency_key == "memory_extract:run-1"

    existing = _Session(existing_job_id="existing-job")
    await jobs.enqueue_completed_run_in_transaction(existing, _run(), memory_enabled=True)

    assert existing.added == []


@pytest.mark.parametrize(
    "run,memory_enabled",
    [
        (_run(status="failed"), True),
        (_run(run_scope="temp"), True),
        (_run(parent_run_id="parent-run"), True),
        (_run(conversation_id=None), True),
        (_run(), False),
    ],
)
async def test_enqueue_skips_ineligible_runs(run, memory_enabled):
    session = _Session()

    await jobs.enqueue_completed_run_in_transaction(session, run, memory_enabled=memory_enabled)

    assert session.added == []


async def test_process_one_applies_small_model_deltas_and_completes_atomically(monkeypatch):
    completed = {}

    async def fake_claim(*, owner):
        assert owner == "worker-1"
        return "job-1", "run-1", "conversation-1", "project-1", "user-1"

    async def fake_extract(**kwargs):
        assert kwargs == {
            "conversation_id": "conversation-1",
            "project_id": "project-1",
            "run_id": "run-1",
            "user_id": "user-1",
        }
        return [{"op": "add", "category": "preference", "content": "다크 모드 선호"}]

    async def fake_apply_and_complete(job_id, **kwargs):
        completed.update(job_id=job_id, **kwargs)
        return True

    monkeypatch.setattr(jobs, "_claim_one", fake_claim)
    monkeypatch.setattr(jobs, "generate_memory_if_applicable", fake_extract)
    monkeypatch.setattr(jobs, "_apply_and_complete", fake_apply_and_complete)

    assert await jobs.process_one(owner="worker-1") is True
    assert completed == {
        "job_id": "job-1",
        "owner": "worker-1",
        "project_id": "project-1",
        "user_id": "user-1",
        "ops": [{"op": "add", "category": "preference", "content": "다크 모드 선호"}],
    }


async def test_process_one_does_not_retry_when_another_worker_owns_the_lease(monkeypatch):
    retried = []

    async def fake_claim(*, owner):
        return "job-1", "run-1", "conversation-1", "project-1", "user-1"

    async def fake_extract(**_kwargs):
        return []

    async def fake_apply_and_complete(*_args, **_kwargs):
        return False

    async def fake_retry(*_args, **_kwargs):
        retried.append(True)

    monkeypatch.setattr(jobs, "_claim_one", fake_claim)
    monkeypatch.setattr(jobs, "generate_memory_if_applicable", fake_extract)
    monkeypatch.setattr(jobs, "_apply_and_complete", fake_apply_and_complete)
    monkeypatch.setattr(jobs, "_retry", fake_retry)

    assert await jobs.process_one(owner="worker-1") is True
    assert retried == []


async def test_process_one_retries_transient_extraction_failure(monkeypatch):
    retried = {}

    async def fake_claim(*, owner):
        return "job-1", "run-1", "conversation-1", "project-1", "user-1"

    async def fake_extract(**_kwargs):
        raise RuntimeError("provider unavailable")

    async def fake_retry(job_id, *, owner):
        retried.update(job_id=job_id, owner=owner)

    monkeypatch.setattr(jobs, "_claim_one", fake_claim)
    monkeypatch.setattr(jobs, "generate_memory_if_applicable", fake_extract)
    monkeypatch.setattr(jobs, "_retry", fake_retry)

    assert await jobs.process_one(owner="worker-1") is True
    assert retried == {"job_id": "job-1", "owner": "worker-1"}


async def test_process_one_retries_post_apply_failure(monkeypatch):
    retried = {}

    async def fake_claim(*, owner):
        return "job-1", "run-1", "conversation-1", "project-1", "user-1"

    async def fake_extract(**_kwargs):
        return [{"op": "add", "category": "preference", "content": "다크 모드 선호"}]

    async def failing_apply_and_complete(*_args, **_kwargs):
        raise RuntimeError("database transaction rolled back")

    async def fake_retry(job_id, *, owner):
        retried.update(job_id=job_id, owner=owner)

    monkeypatch.setattr(jobs, "_claim_one", fake_claim)
    monkeypatch.setattr(jobs, "generate_memory_if_applicable", fake_extract)
    monkeypatch.setattr(jobs, "_apply_and_complete", failing_apply_and_complete)
    monkeypatch.setattr(jobs, "_retry", fake_retry)

    assert await jobs.process_one(owner="worker-1") is True
    assert retried == {"job_id": "job-1", "owner": "worker-1"}


async def test_post_apply_crash_rolls_back_the_memory_and_completion_transaction(monkeypatch):
    state = {"rolled_back": False, "applied": False}

    class PostApplyCrashJob:
        lease_owner = "worker-1"
        lease_expires_at = object()
        progress = None

        def __init__(self):
            self._status = "running"

        @property
        def status(self):
            return self._status

        @status.setter
        def status(self, value):
            if value == "completed":
                raise RuntimeError("simulated crash after memory mutation")
            self._status = value

    class Transaction:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, _exc, _traceback):
            state["rolled_back"] = exc_type is not None
            return False

    class Session:
        def __init__(self):
            self.job = PostApplyCrashJob()

        def begin(self):
            return Transaction()

        async def get(self, _model, _id, **_kwargs):
            return self.job

    session = Session()

    async def fake_apply(current_session, **_kwargs):
        assert current_session is session
        state["applied"] = True
        return {"add": 1, "update": 0, "delete": 0}

    monkeypatch.setattr("lumen.db.get_session_factory", lambda: lambda: Transaction())
    monkeypatch.setattr(jobs.ms, "apply_automatic_ops_in_transaction", fake_apply)

    with pytest.raises(RuntimeError, match="simulated crash"):
        await jobs._apply_and_complete(
            "job-1",
            owner="worker-1",
            project_id="project-1",
            user_id="user-1",
            ops=[{"op": "add", "category": "preference", "content": "다크 모드 선호"}],
        )

    assert state == {"rolled_back": True, "applied": True}
    assert session.job.status == "running"


async def test_title_provider_started_expiry_never_replays_provider(monkeypatch):
    calls = []

    async def fake_claim(*, owner):
        return {"terminal": True}

    async def forbidden_generate(**_kwargs):
        calls.append(True)
        raise AssertionError("provider must not be called after provider-started lease expiry")

    monkeypatch.setattr(title_jobs, "_claim_one", fake_claim)
    monkeypatch.setattr(title_jobs.title_summary, "generate_title", forbidden_generate)

    assert await title_jobs.process_one(owner="worker-1") is True
    assert calls == []


async def test_title_completed_result_replays_without_provider(monkeypatch):
    applied = []
    payload = {
        "expected_title_revision": 1,
        "exchange": [{"role": "user", "content": "질문"}, {"role": "assistant", "content": "답변"}],
    }
    claimed = {
        "job_id": "job-1",
        "run_id": "run-1",
        "conversation_id": "conversation-1",
        "owner": "worker-1",
        "payload": payload,
        "replay": True,
        "attempts": 1,
    }

    async def fake_claim(*, owner):
        assert owner == "worker-1"
        return claimed

    async def fake_apply(job):
        applied.append(job["job_id"])
        return True

    async def forbidden_generate(**_kwargs):
        raise AssertionError("completed durable result must be replayed")

    monkeypatch.setattr(title_jobs, "_claim_one", fake_claim)
    monkeypatch.setattr(title_jobs, "_apply_result", fake_apply)
    monkeypatch.setattr(title_jobs.title_summary, "generate_title", forbidden_generate)

    assert await title_jobs.process_one(owner="worker-1") is True
    assert applied == ["job-1"]


async def test_title_late_result_after_compaction_records_usage_but_does_not_overwrite(monkeypatch):
    payload = {
        "conversation_id": "conversation-1",
        "project_id": "project-1",
        "user_id": "user-1",
        "run_id": "run-1",
        "expected_title_revision": 1,
        "summary_route": {"model_name": "title-model", "provider_name": "provider"},
        "pricing_snapshot": {"input_price_per_token": "0", "output_price_per_token": "0", "margin_multiplier": "1"},
        "result": {"title": "오래된 제목", "prompt_tokens": 3, "completion_tokens": 2, "model_name": "title-model"},
    }

    class Session:
        def __init__(self):
            self.job = SimpleNamespace(
                id="job-1",
                conversation_id="conversation-1",
                status="running",
                lease_owner="worker-1",
                lease_expires_at=object(),
                payload="encrypted",
                progress=None,
                error_code=None,
            )
            self.conversation = SimpleNamespace(
                id="conversation-1",
                title="existing",
                title_source="auto",
                title_status="ready",
                title_revision=2,
            )

        def begin(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, model, _key, **_kwargs):
            if model is title_jobs.ChatJob:
                return self.job
            return self.conversation

        async def execute(self, _statement):
            return _Result(None)

    session = Session()
    monkeypatch.setattr("lumen.db.get_session_factory", lambda: lambda: session)
    monkeypatch.setattr(title_jobs, "_json_load", lambda _ciphertext: payload)
    monkeypatch.setattr(title_jobs.litellm_client, "cost_from_usage", lambda *_args, **_kwargs: object())
    usage = []

    async def fake_apply_usage(_session, **kwargs):
        usage.append(kwargs["event_id"])
        return 0

    monkeypatch.setattr(title_jobs.credit, "apply_usage_in_transaction", fake_apply_usage)
    assert (
        await title_jobs._apply_result({"job_id": "job-1", "owner": "worker-1", "payload": payload, "replay": True})
        is True
    )
    assert session.conversation.title == "existing"
    assert session.job.status == "completed"
    assert usage == ["title:conversation-1:1"]


async def test_title_apply_failure_requeues_stored_result_without_provider_retry(monkeypatch):
    claimed = {
        "job_id": "job-1",
        "owner": "worker-1",
        "payload": {
            "expected_title_revision": 1,
            "user_id": "user-1",
            "project_id": "project-1",
            "exchange": [{"role": "user", "content": "질문"}, {"role": "assistant", "content": "답변"}],
            "summary_route": {"model_name": "title-model"},
        },
        "replay": False,
        "attempts": 1,
    }
    provider_calls = []
    requeued = []

    async def fake_claim(*, owner):
        assert owner == "worker-1"
        return claimed

    async def fake_precheck(*_args):
        return None

    async def fake_started(*_args, **_kwargs):
        return None

    async def fake_generate(**_kwargs):
        provider_calls.append(True)
        return SimpleNamespace(title="제목", prompt_tokens=2, completion_tokens=1, model_name="title-model")

    async def fake_store(*_args):
        return None

    async def failing_apply(*_args, **_kwargs):
        raise RuntimeError("apply transaction rolled back")

    async def fake_requeue(job_id, *, owner):
        requeued.append((job_id, owner))

    monkeypatch.setattr(title_jobs, "_claim_one", fake_claim)
    monkeypatch.setattr(title_jobs.credit, "precheck", fake_precheck)
    monkeypatch.setattr(title_jobs, "_mark_provider_started", fake_started)
    monkeypatch.setattr(title_jobs.title_summary, "generate_title", fake_generate)
    monkeypatch.setattr(title_jobs, "_store_result", fake_store)
    monkeypatch.setattr(title_jobs, "_apply_result", failing_apply)
    monkeypatch.setattr(title_jobs, "_requeue_stored_result", fake_requeue)

    assert await title_jobs.process_one(owner="worker-1") is True
    assert provider_calls == [True]
    assert requeued == [("job-1", "worker-1")]
