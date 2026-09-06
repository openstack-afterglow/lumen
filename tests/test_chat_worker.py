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


async def test_worker_owns_checkpointer_lifecycle(monkeypatch):
    fake = _FakeCheckpointer()
    monkeypatch.setattr(
        chat_worker,
        "get_settings",
        lambda: SimpleNamespace(
            database_url="mysql+aiomysql://test",
            database_pool_size=1,
            database_max_overflow=0,
            database_connect_timeout=10,
            database_pool_timeout=10,
            database_unhealthy_seconds=15,
            chat_checkpointer_postgres_url="postgresql://checkpoint",
            chat_semantic_memory_enabled=False,
        ),
    )
    monkeypatch.setattr(chat_worker, "init_db", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(checkpointer, "chat_checkpointer", fake)

    async def stop_worker():
        raise asyncio.CancelledError

    import asyncio

    monkeypatch.setattr(chat_worker, "_next_run_ids", stop_worker)
    monkeypatch.setattr("lumen.db.close_db", stop_worker)

    with pytest.raises(asyncio.CancelledError):
        await chat_worker.serve()

    assert fake.started_with == ["postgresql://checkpoint"]
    assert fake.closed is True


async def test_worker_sweeps_expired_v2_inputs_without_client_reconnect(monkeypatch):
    import asyncio

    fake = _FakeCheckpointer()
    monkeypatch.setattr(
        chat_worker,
        "get_settings",
        lambda: SimpleNamespace(
            database_url="mysql+aiomysql://test",
            database_pool_size=1,
            database_max_overflow=0,
            database_connect_timeout=10,
            database_pool_timeout=10,
            database_unhealthy_seconds=15,
            chat_checkpointer_postgres_url=None,
            chat_semantic_memory_enabled=False,
        ),
    )
    monkeypatch.setattr(chat_worker, "init_db", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(checkpointer, "chat_checkpointer", fake)
    sweeps: list[int] = []

    async def expire_inputs() -> list[str]:
        sweeps.append(1)
        return []

    async def stop_worker():
        raise asyncio.CancelledError

    monkeypatch.setattr(chat_worker, "expire_pending_inputs", expire_inputs)
    monkeypatch.setattr(chat_worker, "_next_run_ids", stop_worker)
    monkeypatch.setattr("lumen.db.close_db", stop_worker)

    with pytest.raises(asyncio.CancelledError):
        await chat_worker.serve()

    assert sweeps == [1]


async def test_worker_dispatches_independent_title_job_task(monkeypatch):
    import asyncio

    fake = _FakeCheckpointer()
    monkeypatch.setattr(
        chat_worker,
        "get_settings",
        lambda: SimpleNamespace(
            database_url="mysql+aiomysql://test",
            database_pool_size=1,
            database_max_overflow=0,
            database_connect_timeout=10,
            database_pool_timeout=10,
            database_unhealthy_seconds=15,
            chat_checkpointer_postgres_url=None,
            chat_semantic_memory_enabled=False,
        ),
    )
    monkeypatch.setattr(chat_worker, "init_db", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(checkpointer, "chat_checkpointer", fake)
    called = []

    async def fake_title_processor(*, owner):
        called.append(owner)
        return False

    async def stop_worker():
        raise asyncio.CancelledError

    monkeypatch.setattr("lumen.services.title_jobs.process_one", fake_title_processor)
    monkeypatch.setattr(chat_worker, "_next_run_ids", stop_worker)
    monkeypatch.setattr("lumen.db.close_db", stop_worker)

    with pytest.raises(asyncio.CancelledError):
        await chat_worker.serve()

    assert called


async def test_title_processor_loop_keeps_draining_until_cancelled():
    import asyncio

    calls = []

    async def processor(*, owner):
        calls.append(owner)
        if len(calls) == 1:
            return True
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await chat_worker._title_processor_loop(processor, owner="worker-1")

    assert calls == ["worker-1", "worker-1"]
