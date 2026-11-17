"""Exercise real controller retry/submission logic with synthetic SQL boundaries."""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import OperationalError

from lumen.services.infrastructure import controller as controller_module


class _Session:
    """One disposable transaction; store._locked_resource still issues its real reads."""

    def __init__(self, harness):
        self.harness = harness
        self.number = len(harness.sessions) + 1
        self.connection_options = []
        self.statements = []
        self.active = False
        self.closed = False
        self.outcome = None
        harness.sessions.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        assert not self.active
        self.closed = True

    @asynccontextmanager
    async def begin(self):
        assert not self.active
        self.active = True
        try:
            yield self
        except BaseException:
            self.outcome = "rolled-back"
            raise
        else:
            if self.harness.commit_conflict:
                self.outcome = "commit-conflict"
                raise self.harness.conflict
            self.outcome = "committed"
        finally:
            self.active = False

    async def connection(self, *, execution_options):
        assert self.active
        assert not self.statements, "isolation must be set before the first SQL statement"
        assert not self.connection_options, "each transaction must start on a fresh session"
        assert execution_options == {"isolation_level": "READ COMMITTED"}
        self.connection_options.append(execution_options)
        return SimpleNamespace()

    async def execute(self, statement):
        assert self.active
        assert self.connection_options == [{"isolation_level": "READ COMMITTED"}]
        self.statements.append(statement)
        if len(self.statements) == 1 and self.number <= self.harness.prefix_conflicts:
            raise self.harness.conflict
        values = (self.harness.resource.pool_id, self.harness.lease, self.harness.resource)
        value = values[len(self.statements) - 1]
        return SimpleNamespace(scalar_one=lambda: value, scalar_one_or_none=lambda: value)


@pytest.fixture(params=["create", "delete"])
def effect_harness(request, monkeypatch):
    controller = controller_module.ResourceController(
        SimpleNamespace(max_parallel_cloud_operations=2, pools=(), tls=None), {}, owner="controller-owner",
    )
    action = request.param
    resource = SimpleNamespace(
        id="owned-resource", pool_id="owned-pool", generation=7, role="worker",
        desired_state="requested" if action == "create" else "deleting",
        observed_state="unknown" if action == "create" else "booting",
        ready_at=None, drain_requested_at=None, drain_ack_at=None, failure_code=None,
    )
    harness = SimpleNamespace(
        controller=controller, resource=resource, action=action, fence=11,
        lease=SimpleNamespace(
            reconcile_lease_owner=controller.owner, reconcile_fence=11,
            reconcile_lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
        sessions=[], calls=[], native_threads=[], result=object(), prefix_conflicts=0,
        native_error=None, commit_conflict=False,
        conflict=OperationalError(
            "synthetic SQL boundary", {}, RuntimeError(1020, "Record has changed since last read"),
        ),
        unknown=AsyncMock(return_value=True), deferred=AsyncMock(return_value=True),
    )

    def session_factory():
        return _Session(harness)

    def native(*args):
        harness.calls.append(args)
        harness.native_threads.append(threading.get_ident())
        if harness.native_error is not None:
            raise harness.native_error
        return harness.result

    harness.native = native
    monkeypatch.setattr(controller_module, "get_session_factory", lambda: session_factory)
    monkeypatch.setattr(controller_module.store, "mark_unknown", harness.unknown)
    monkeypatch.setattr(controller_module.store, "defer_unsubmitted_operation", harness.deferred)
    try:
        yield harness
    finally:
        controller.executor.shutdown(wait=True)


@pytest.mark.parametrize("failure_at", ["first-sql", "before-native-thread"])
async def test_unsubmitted_conflict_retries_fresh_transaction_and_calls_native_once(
    effect_harness, monkeypatch, failure_at,
):
    h = effect_harness
    if failure_at == "first-sql":
        h.prefix_conflicts = 1
    else:
        settled = h.controller._settled
        submissions = 0

        async def before_thread(function, *args):
            nonlocal submissions
            submissions += 1
            if submissions == 1:
                raise h.conflict
            return await settled(function, *args)

        monkeypatch.setattr(h.controller, "_settled", before_thread)

    result = await h.controller._claimed_effect(h.resource, h.fence, h.action, h.native, "native-request")

    assert result is h.result
    assert h.calls == [("native-request",)]
    assert len(h.native_threads) == 1 and h.native_threads[0] != threading.get_ident()
    assert len(h.sessions) == 2 and h.sessions[0] is not h.sessions[1]
    assert [session.outcome for session in h.sessions] == ["rolled-back", "committed"]
    assert [len(session.statements) for session in h.sessions] == [1 if failure_at == "first-sql" else 3, 3]
    assert all(session.connection_options == [{"isolation_level": "READ COMMITTED"}] for session in h.sessions)
    assert all(session.closed and not session.active for session in h.sessions)
    h.unknown.assert_not_awaited()
    h.deferred.assert_not_awaited()


@pytest.mark.parametrize("failure_at", ["native", "post-submit", "commit"])
async def test_submitted_conflict_never_replays_native_and_marks_claim_unknown(
    effect_harness, monkeypatch, failure_at,
):
    h = effect_harness
    if failure_at == "native":
        h.native_error = h.conflict
    elif failure_at == "post-submit":
        settled = h.controller._settled

        async def after_native(function, *args):
            result = await settled(function, *args)
            assert result is h.result
            raise h.conflict

        monkeypatch.setattr(h.controller, "_settled", after_native)
    else:
        h.commit_conflict = True

    with pytest.raises(OperationalError) as caught:
        await h.controller._claimed_effect(h.resource, h.fence, h.action, h.native, "native-request")

    assert caught.value is h.conflict
    assert h.calls == [("native-request",)]
    assert len(h.native_threads) == 1 and h.native_threads[0] != threading.get_ident()
    assert len(h.sessions) == 1
    session = h.sessions[0]
    assert len(session.statements) == 3
    assert session.outcome == ("commit-conflict" if failure_at == "commit" else "rolled-back")
    assert session.closed and not session.active
    h.unknown.assert_awaited_once_with(
        h.resource.id, h.resource.generation, h.controller.owner, h.fence, action=h.action,
    )
    h.deferred.assert_not_awaited()


async def test_prefix_conflict_exhaustion_is_bounded_and_defers_unsubmitted_claim(effect_harness):
    h = effect_harness
    h.prefix_conflicts = 3

    with pytest.raises(OperationalError) as caught:
        await h.controller._claimed_effect(h.resource, h.fence, h.action, h.native, "native-request")

    assert caught.value is h.conflict
    assert h.calls == [] and h.native_threads == []
    assert len(h.sessions) == 3
    assert len({id(session) for session in h.sessions}) == 3
    assert all(len(session.statements) == 1 and session.outcome == "rolled-back" for session in h.sessions)
    assert all(session.connection_options == [{"isolation_level": "READ COMMITTED"}] for session in h.sessions)
    assert all(session.closed and not session.active for session in h.sessions)
    h.deferred.assert_awaited_once_with(
        h.resource.id, h.resource.generation, h.action, h.controller.owner, h.fence,
    )
    h.unknown.assert_not_awaited()
