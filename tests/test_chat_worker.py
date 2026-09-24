import asyncio
from types import SimpleNamespace

import pytest

from lumen import worker as chat_worker
from lumen.services import checkpointer


class _FakeCheckpointer:
    def __init__(self) -> None:
        self.started_with: list[str] = []
        self.closed = False

    async def start(self, dsn: str) -> bool:
        self.started_with.append(dsn)
        return True

    async def close(self) -> None:
        self.closed = True


def _settings(**overrides):
    base = dict(
        database_url="mysql+aiomysql://test",
        database_pool_size=1,
        database_max_overflow=0,
        database_connect_timeout=10,
        database_pool_timeout=10,
        database_unhealthy_seconds=15,
        chat_checkpointer_postgres_url=None,
        chat_semantic_memory_enabled=False,
        worker_concurrency=4,
        worker_heartbeat_seconds=5,
        worker_drain_seconds=300,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeRegistry:
    digest = "0" * 64
    ready = True

    def load(self) -> None:
        return None

    async def start(self, host) -> None:
        return None

    async def close(self) -> None:
        return None


def _patch_serve_dependencies(monkeypatch, fake_checkpointer, **settings_overrides):
    monkeypatch.setattr(chat_worker, "get_settings", lambda: _settings(**settings_overrides))
    monkeypatch.setattr(chat_worker, "init_db", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chat_worker, "get_registry", lambda: _FakeRegistry())
    monkeypatch.setattr(chat_worker, "build_host", lambda: None)
    monkeypatch.setattr(checkpointer, "chat_checkpointer", fake_checkpointer)

    async def register(self) -> None:
        self.registration_id = "registration-1"

    async def heartbeat(self) -> None:
        return None

    monkeypatch.setattr(chat_worker.WorkerLoop, "register", register)
    monkeypatch.setattr(chat_worker.WorkerLoop, "heartbeat", heartbeat)

    async def no_recovery(*, owner):
        return []

    monkeypatch.setattr(chat_worker, "recover_stale_runs", no_recovery)


async def test_worker_owns_checkpointer_lifecycle(monkeypatch):
    fake = _FakeCheckpointer()
    _patch_serve_dependencies(monkeypatch, fake, chat_checkpointer_postgres_url="postgresql://checkpoint")

    async def stop_worker(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(chat_worker, "_next_run_ids", stop_worker)
    monkeypatch.setattr("lumen.db.close_db", stop_worker)

    with pytest.raises(asyncio.CancelledError):
        await chat_worker.serve()

    assert fake.started_with == ["postgresql://checkpoint"]
    assert fake.closed is True


async def test_slow_maintenance_never_blocks_claims(monkeypatch):
    """A stuck maintenance iteration must not delay polling or dispatch of queued runs."""
    fake = _FakeCheckpointer()
    _patch_serve_dependencies(monkeypatch, fake)
    sweeps: list[int] = []
    claims: list[str] = []
    executed: list[str] = []

    async def expire_inputs() -> list[str]:
        sweeps.append(1)
        await asyncio.sleep(3600)  # never finishes during the test
        return []

    async def next_run_ids(limit: int) -> list[str]:
        claims.append(f"claim-{len(claims)}")
        if len(claims) == 1:
            return ["run-1"]
        raise asyncio.CancelledError

    async def execute_run(run_id: str, *, owner: str, registration_id: str | None = None) -> bool:
        assert registration_id == "registration-1"
        executed.append(run_id)
        return True

    monkeypatch.setattr(chat_worker, "expire_pending_inputs", expire_inputs)
    monkeypatch.setattr(chat_worker, "_next_run_ids", next_run_ids)
    monkeypatch.setattr(chat_worker, "execute_queued_run", execute_run)

    async def close_db():
        return None

    monkeypatch.setattr("lumen.db.close_db", close_db)

    with pytest.raises(asyncio.CancelledError):
        await chat_worker.serve()

    assert sweeps == [1]
    assert executed == ["run-1"]
    assert len(claims) == 2


async def test_worker_dispatches_independent_title_job_task(monkeypatch):
    fake = _FakeCheckpointer()
    _patch_serve_dependencies(monkeypatch, fake)
    called = []

    async def fake_title_processor(*, owner):
        called.append(owner)
        await asyncio.sleep(0)
        return False

    async def stop_worker(*_args, **_kwargs):
        await asyncio.sleep(0)
        raise asyncio.CancelledError

    monkeypatch.setattr("lumen.services.title_jobs.process_one", fake_title_processor)
    monkeypatch.setattr(chat_worker, "_next_run_ids", stop_worker)
    monkeypatch.setattr("lumen.db.close_db", stop_worker)

    with pytest.raises(asyncio.CancelledError):
        await chat_worker.serve()

    assert called


async def test_title_processor_loop_keeps_draining_until_cancelled():
    calls: list[str] = []

    async def processor(*, owner: str) -> bool:
        calls.append(owner)
        if len(calls) >= 2:
            raise asyncio.CancelledError
        return True

    with pytest.raises(asyncio.CancelledError):
        await chat_worker._title_processor_loop(processor, owner="worker-1")

    assert calls == ["worker-1", "worker-1"]


async def test_worker_loop_refills_freed_slot_and_drain_rejects_new_claims(monkeypatch):
    """Capacity bounds concurrent runs, a finished run frees its slot at once, drain stops claiming."""
    gates: dict[str, asyncio.Event] = {}

    async def execute_run(run_id: str, *, owner: str, registration_id: str | None = None) -> bool:
        assert registration_id is None
        gates[run_id] = asyncio.Event()
        await gates[run_id].wait()
        return True

    monkeypatch.setattr(chat_worker, "execute_queued_run", execute_run)
    loop = chat_worker.WorkerLoop(owner="w", capacity=2, heartbeat_seconds=5, drain_seconds=300)

    loop.launch("a")
    loop.launch("b")
    loop.launch("c")  # over capacity: ignored
    await asyncio.sleep(0)
    assert set(loop.active) == {"a", "b"} and loop.free_slots == 0

    gates["a"].set()
    await asyncio.gather(loop.active["a"])
    assert loop.free_slots == 1 and "a" not in loop.active

    loop.start_drain()
    loop.launch("d")
    assert "d" not in loop.active and loop.free_slots == 0
    # Work already in flight is preserved through the drain rather than killed.
    assert "b" in loop.active and not loop.active["b"].done()
    assert loop.drain_overdue() is False
    loop.drain_started_at -= 301
    assert loop.drain_overdue() is True

    gates["b"].set()
    await asyncio.gather(loop.active["b"])
    assert not loop.active


async def test_lost_registration_enters_drain(monkeypatch):
    loop = chat_worker.WorkerLoop(owner="w", capacity=1, heartbeat_seconds=5, drain_seconds=300)
    loop.registration_id = "registration-1"

    async def heartbeat_worker(registration_id, *, active_count, accepting):
        return False

    monkeypatch.setattr("lumen.services.infrastructure.store.heartbeat_worker", heartbeat_worker, raising=False)
    await loop.heartbeat()
    assert loop.draining.is_set()
    assert loop.free_slots == 0
