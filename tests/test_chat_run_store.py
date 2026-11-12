from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from lumen.models.chat_infrastructure import ChatWorkerRegistration
from lumen.models.chat_runs import ChatRun, ChatRunSegment
from lumen.services.run_store import (
    RunStoreError,
    begin_segment_io,
    claim_finalizer,
    complete_segment_io,
    fail_unresolved_segment,
    load_segment_payload,
    record_segment_usage,
    renew_run_lease,
    replay_events,
    transition_run,
)


def _run(status: str) -> ChatRun:
    return ChatRun(
        id="run-1",
        run_scope="persistent",
        project_id="project-1",
        user_id="user-1",
        model_name="model",
        capability_snapshot={},
        pricing_snapshot={},
        client_request_id="00000000-0000-0000-0000-000000000001",
        request_fingerprint="fingerprint",
        fingerprint_version=1,
        status=status,
        reserved_credits=Decimal("0"),
    )


def _segment(status: str = "prepared", *, endpoint: str = "chat_completions") -> ChatRunSegment:
    return ChatRunSegment(
        run_id="run-1",
        segment_id="executor:1",
        ordinal=1,
        endpoint=endpoint,
        status=status,
    )


class _ReplayResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self.rows


class _ReplaySession:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _statement):
        return _ReplayResult(self.rows)


@pytest.mark.asyncio
async def test_replay_events_marks_mysql_naive_timestamps_as_utc():
    from lumen.crypto import encrypt_chat_content

    run = _run("running")
    run.last_seq = 1
    row = SimpleNamespace(
        seq=1,
        event_type="run.stage.changed",
        created_at=datetime(2026, 7, 23, 14, 0, 0),
        payload=encrypt_chat_content('{"stage":"model_request","tool_name":null}'),
    )

    event = (await replay_events(_ReplaySession([row]), run, after_seq=0))[0]

    assert event.created_at == datetime(2026, 7, 23, 14, 0, 0, tzinfo=UTC)


def test_segment_write_ahead_fence_prevents_reissuing_unknown_external_calls():
    segment = _segment()
    begin_segment_io(segment)
    assert segment.status == "provider_started"
    assert segment.provider_started_at is not None

    with pytest.raises(RunStoreError, match="cannot begin"):
        begin_segment_io(segment)

    fail_unresolved_segment(segment, error_code="provider_result_unknown")
    assert segment.status == "failed"
    assert load_segment_payload(segment.result_payload) == {"code": "provider_result_unknown"}
    with pytest.raises(RunStoreError, match="cannot complete"):
        complete_segment_io(segment, result_payload={"text": "result"}, usage_payload=None)


def test_segment_completion_requires_committed_started_fence():
    segment = _segment()
    with pytest.raises(RunStoreError, match="cannot complete"):
        complete_segment_io(segment, result_payload={"text": "result"}, usage_payload=None)
    begin_segment_io(segment)
    complete_segment_io(
        segment,
        result_payload={"text": "result"},
        usage_payload={"prompt_tokens": 1, "completion_tokens": 2},
    )
    assert segment.status == "completed"
    assert load_segment_payload(segment.result_payload) == {"text": "result"}
    assert load_segment_payload(segment.usage_payload) == {"completion_tokens": 2, "prompt_tokens": 1}


def test_provider_segments_allow_canonical_output_limit_while_tools_remain_bounded():
    payload = {"text": "x" * (128 * 1024)}

    provider = _segment()
    begin_segment_io(provider)
    complete_segment_io(provider, result_payload=payload, usage_payload=None)
    assert load_segment_payload(provider.result_payload) == payload

    tool = _segment(endpoint="tool")
    begin_segment_io(tool)
    with pytest.raises(RunStoreError, match="exceeds"):
        complete_segment_io(tool, result_payload=payload, usage_payload=None)


def test_paid_usage_checkpoint_does_not_complete_or_reopen_provider_io():
    segment = _segment()
    begin_segment_io(segment)
    started_at = segment.provider_started_at
    record_segment_usage(segment, {"input_tokens": 120, "output_tokens": 80})
    checkpoint = segment.usage_payload
    assert checkpoint is not None and '"input_tokens"' not in checkpoint
    assert segment.status == "provider_started"
    assert segment.provider_started_at == started_at
    assert segment.completed_at is None and segment.result_payload is None
    with pytest.raises(RunStoreError):
        begin_segment_io(segment)

    fail_unresolved_segment(segment, error_code="provider_result_unknown")
    assert segment.status == "failed"
    assert segment.usage_payload == checkpoint
    assert load_segment_payload(segment.usage_payload)["output_tokens"] == 80
    with pytest.raises(RunStoreError):
        record_segment_usage(segment, {"input_tokens": 0, "output_tokens": 0})
    assert segment.usage_payload == checkpoint


@pytest.mark.parametrize("status", ["prepared", "completed", "failed"])
def test_usage_checkpoint_cannot_overwrite_an_unstarted_or_terminal_segment(status):
    segment = _segment(status)
    with pytest.raises(RunStoreError):
        record_segment_usage(segment, {"input_tokens": 120, "output_tokens": 80})
    assert segment.status == status
    assert segment.usage_payload is None
    assert segment.provider_started_at is None and segment.completed_at is None


def test_oversize_utf8_usage_cannot_destroy_paid_checkpoint_or_complete_segment():
    segment = _segment()
    begin_segment_io(segment)
    record_segment_usage(segment, {"input_tokens": 120, "output_tokens": 80})
    checkpoint = segment.usage_payload
    with pytest.raises(RunStoreError):
        complete_segment_io(
            segment,
            result_payload={"text": "stored output"},
            usage_payload={"evidence": "한" * (6 * 1024)},
        )
    assert segment.status == "provider_started"
    assert segment.usage_payload == checkpoint
    assert segment.result_payload is None and segment.completed_at is None


@pytest.mark.asyncio
async def test_run_state_transitions_are_strict_and_terminal_only_from_finalizing():
    run = _run("queued")
    await transition_run(None, run, expected="queued", target="running")
    await transition_run(None, run, expected="running", target="finalizing")
    await transition_run(None, run, expected="finalizing", target="completed")
    assert run.status == "completed"

    with pytest.raises(RunStoreError, match="illegal"):
        await transition_run(None, _run("queued"), expected="queued", target="completed")


@pytest.mark.asyncio
async def test_run_transition_rejects_stale_owner_state():
    with pytest.raises(RunStoreError, match="expected"):
        await transition_run(None, _run("running"), expected="queued", target="running")


class _ScalarResult:
    def __init__(self, run):
        self.run = run

    def scalar_one_or_none(self):
        return self.run

    def one_or_none(self):
        return self.run


class _LeaseSession:
    """Answers entity selects with the stored row and column selects with its attributes."""

    def __init__(self, run, registration=None):
        self.run = run
        self.registration = registration

    async def execute(self, statement):
        descriptions = statement.column_descriptions
        entity = descriptions[0]["entity"]
        source = self.registration if entity is ChatWorkerRegistration else self.run
        if len(descriptions) == 1 and descriptions[0]["expr"] is entity:
            return _ScalarResult(source)
        if source is None:
            return _ScalarResult(None)
        values = {description["name"]: getattr(source, description["name"]) for description in descriptions}
        if len(descriptions) == 1:
            return _ScalarResult(next(iter(values.values())))
        return _ScalarResult(SimpleNamespace(**values))

    async def scalar(self, _statement):
        # No other live run leases hold this registration's capacity.
        return 0


@pytest.mark.asyncio
async def test_renew_run_lease_requires_current_running_owner():
    run = _run("running")
    run.lease_owner = "worker-a"
    from datetime import UTC, datetime, timedelta

    run.lease_expires_at = datetime.now(UTC) + timedelta(seconds=1)
    renewed = await renew_run_lease(_LeaseSession(run), run.id, owner="worker-a")

    assert renewed is run
    assert run.lease_expires_at is not None
    assert await renew_run_lease(_LeaseSession(run), run.id, owner="worker-b") is None
    run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await renew_run_lease(_LeaseSession(run), run.id, owner="worker-a") is None
    run.lease_expires_at = None
    assert await renew_run_lease(_LeaseSession(run), run.id, owner="worker-a") is None
    run.status = "running"
    run.lease_expires_at = (datetime.now(UTC) + timedelta(seconds=1)).replace(tzinfo=None)
    assert await renew_run_lease(_LeaseSession(run), run.id, owner="worker-a") is run
    run.status = "finalizing"
    assert await renew_run_lease(_LeaseSession(run), run.id, owner="worker-a") is None


@pytest.mark.asyncio
async def test_claim_fence_invalidates_stale_owner_after_reclaim():
    """A lapsed owner's token must not renew once a later claim advanced the fence."""
    from lumen.services.run_store import claim_queued_run

    run = _run("queued")
    run.lease_fence = 0
    run.workload_class = "online_text"
    run.execution_protocol_version = 1
    registration = ChatWorkerRegistration(
        id="registration-1", worker_identity="worker-a", boot_id="boot-1", resource_id=None, pool_id=None,
        protocol_versions=[1], workload_classes=["online_text"], plugin_digest="d" * 64, schema_version=1,
        capacity=1, accepting=True, draining=False, heartbeat_at=datetime.now(UTC),
    )
    session = _LeaseSession(run, registration)
    first = await claim_queued_run(session, run.id, owner="worker-a", registration_id=registration.id)
    stale_token = first.lease_owner
    assert run.lease_fence == 1 and stale_token.endswith("#1")
    assert run.worker_registration_id == registration.id

    # Lease lapses and recovery requeues the run; a fresh worker claims it again.
    run.status = "queued"
    run.lease_owner = None
    run.lease_expires_at = None
    run.worker_registration_id = None
    second = await claim_queued_run(session, run.id, owner="worker-a", registration_id=registration.id)
    assert run.lease_fence == 2 and second.lease_owner != stale_token

    assert await renew_run_lease(session, run.id, owner=stale_token) is None
    assert await renew_run_lease(session, run.id, owner=second.lease_owner) is run

    # A different workload class never claims the run, whatever its free capacity.
    run.status = "queued"
    run.lease_owner = None
    registration.workload_classes = ["batch"]
    assert await claim_queued_run(session, run.id, owner="worker-a", registration_id=registration.id) is None
    assert run.lease_fence == 2

@pytest.mark.asyncio
async def test_finalizer_claim_accepts_persisted_naive_expiry():
    from datetime import UTC, datetime, timedelta

    run = _run("running")
    run.finalizer_lease_owner = "worker-a"
    run.finalizer_lease_expires_at = (datetime.now(UTC) + timedelta(seconds=1)).replace(tzinfo=None)

    assert await claim_finalizer(_LeaseSession(run), run.id, owner="worker-b") is None
    claimed = await claim_finalizer(_LeaseSession(run), run.id, owner="worker-a")
    assert claimed is run
    assert run.status == "finalizing"
