"""Durable post-response memory extraction job tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lumen.services import memory_jobs as jobs

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
