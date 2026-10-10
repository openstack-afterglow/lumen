"""Octavia writes are intents, not proof of application or member ownership."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from openstack.exceptions import ResourceNotFound

from lumen.services.infrastructure.config import IngressConfig
from lumen.services.infrastructure.ingress import IngressCreateDeferred, IngressProvider, IngressUnknownCreate

RESOURCE = "39b979c1-cf91-40c8-b8a4-2d755196bf40"
ADDRESS = "10.42.0.8"
GENERATION = 3


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Octavia:
    """SDK-shaped fake: 202 writes settle later and may lose their response."""

    def __init__(self):
        self.rows = []
        self.calls = []
        self.pending_reads = 0
        self.pending_action = None
        self.pool_status = "ACTIVE"
        self.never_active = False
        self.lose_response = set()
        self.hide_created = False
        self.fail_reads_after_create = False

    def members(self, pool):
        assert pool == "pool"
        self.calls.append(("members",))
        if self.fail_reads_after_create and any(call[0] == "create" for call in self.calls):
            raise ConnectionError("read response lost")
        if self.pending_action is not None:
            if self.pending_reads == 0 and not self.never_active:
                self.pending_action()
                self.pending_action = None
                self.pool_status = "ACTIVE"
            else:
                self.pending_reads -= 1
        return [SimpleNamespace(**deepcopy(row)) for row in self.rows]

    def get_member(self, member, pool):
        assert pool == "pool"
        self.calls.append(("get_member", member))
        row = next((row for row in self.rows if row["id"] == member), None)
        if row is None:
            raise ResourceNotFound("member absent")
        return SimpleNamespace(**deepcopy(row))

    def get_pool(self, pool):
        assert pool == "pool"
        self.calls.append(("pool", self.pool_status))
        return SimpleNamespace(provisioning_status=self.pool_status)

    def _pending(self, action, name):
        self.pending_reads = 2
        self.pending_action = action
        self.pool_status = "PENDING_UPDATE"
        if name in self.lose_response:
            raise ConnectionError("SDK response lost after Octavia accepted the write")

    def create_member(self, pool, **attrs):
        assert pool == "pool"
        self.calls.append(("create", deepcopy(attrs)))
        row = dict(id="member", operating_status="ONLINE", provisioning_status="PENDING_CREATE", **attrs)
        if not self.hide_created:
            self.rows.append(row)
        self._pending(lambda: row.update(provisioning_status="ACTIVE"), "create")
        return SimpleNamespace(**deepcopy(row))

    def update_member(self, member_id, pool, **attrs):
        assert pool == "pool"
        row = next(row for row in self.rows if row["id"] == member_id)
        self.calls.append(("update", member_id, deepcopy(attrs)))
        row["provisioning_status"] = "PENDING_UPDATE"
        self._pending(lambda: row.update(provisioning_status="ACTIVE", **attrs), "update")
        return SimpleNamespace(**deepcopy(row))

    def delete_member(self, member_id, pool, ignore_missing):
        assert pool == "pool" and ignore_missing is True
        row = next(row for row in self.rows if row["id"] == member_id)
        self.calls.append(("delete", member_id))
        row["provisioning_status"] = "PENDING_DELETE"
        self._pending(lambda: self.rows.remove(row), "delete")


def owned_member(**overrides):
    row = dict(
        id="member", name=f"lumen-{RESOURCE}", address=ADDRESS, protocol_port=8012,
        subnet_id="subnet", tags=[f"lumen-resource:{RESOURCE}", f"lumen-generation:{GENERATION}"],
        weight=1, is_admin_state_up=True, operating_status="ONLINE", provisioning_status="ACTIVE",
    )
    return {**row, **overrides}


@pytest.fixture
def setup(monkeypatch):
    import lumen.services.infrastructure.ingress as ingress

    clock = Clock()
    monkeypatch.setattr(ingress.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(ingress.time, "sleep", clock.sleep)
    sdk = Octavia()
    config = IngressConfig(
        ingress_pool_id="pool", ingress_vip="10.42.0.20", ingress_subnet_id="subnet",
        ingress_member_port=8012, api_target_active_requests=8, api_target_ttft_ms=500,
    )
    provider = IngressProvider(SimpleNamespace(load_balancer=sdk), config,
                               timeout_seconds=3, poll_interval_seconds=1)
    return sdk, provider, clock


def test_register_waits_for_member_and_pool_active(setup):
    sdk, provider, clock = setup
    result = provider.register(RESOURCE, GENERATION, ADDRESS, None)
    assert result.id == "member" and result.healthy and result.enabled
    assert result.weight == 1 and result.provisioning_status == "ACTIVE"
    create = next(call for call in sdk.calls if call[0] == "create")
    assert create[1]["weight"] == 1 and create[1]["is_admin_state_up"] is True
    assert create[1]["tags"] == [f"lumen-resource:{RESOURCE}", f"lumen-generation:{GENERATION}"]
    assert clock.sleeps == [1, 1]
    assert sdk.calls[-1] == ("pool", "ACTIVE")


def test_pending_pool_cannot_report_a_healthy_active_member(setup):
    sdk, provider, _ = setup
    sdk.rows = [owned_member()]
    sdk.pool_status = "PENDING_UPDATE"
    assert provider.inspect(RESOURCE, GENERATION, ADDRESS).healthy is False
    sdk.pool_status = "ACTIVE"
    sdk.rows[0]["provisioning_status"] = "PENDING_UPDATE"
    assert provider.inspect(RESOURCE, GENERATION, ADDRESS).healthy is False


def test_drain_keeps_admin_up_and_waits_before_delete(setup):
    sdk, provider, clock = setup
    sdk.rows = [owned_member()]
    provider.drain("member", RESOURCE, GENERATION, ADDRESS)
    assert sdk.rows[0]["weight"] == 0
    assert sdk.rows[0]["is_admin_state_up"] is True
    assert sdk.rows[0]["provisioning_status"] == "ACTIVE"
    assert provider.inspect(RESOURCE, GENERATION, ADDRESS).enabled is False
    assert clock.now == 2
    provider.delete("member", RESOURCE, GENERATION, ADDRESS)
    assert sdk.rows == [] and sdk.pool_status == "ACTIVE"
    assert clock.now == 4
    assert next(call for call in sdk.calls if call[0] == "update")[2] == {
        "weight": 0, "is_admin_state_up": True,
    }


def test_drain_202_pending_update_is_not_success(setup):
    sdk, provider, clock = setup
    sdk.rows = [owned_member()]
    sdk.never_active = True
    with pytest.raises(TimeoutError, match="ACTIVE"):
        provider.drain("member", RESOURCE, GENERATION, ADDRESS)
    assert clock.now == 3
    assert not any(call[0] == "delete" for call in sdk.calls)
    assert len([call for call in sdk.calls if call[0] == "update"]) == 1


@pytest.mark.parametrize("operation", ["create", "update", "delete"])
def test_lost_write_response_is_reconciled_without_repeating_write(setup, operation):
    sdk, provider, _ = setup
    sdk.lose_response.add(operation)
    if operation == "create":
        assert provider.register(RESOURCE, GENERATION, ADDRESS, None).healthy
    elif operation == "update":
        sdk.rows = [owned_member()]
        provider.drain(None, RESOURCE, GENERATION, ADDRESS)
        assert sdk.rows[0]["weight"] == 0
    else:
        sdk.rows = [owned_member(weight=0)]
        provider.delete(None, RESOURCE, GENERATION, ADDRESS)
        assert sdk.rows == []
    assert len([call for call in sdk.calls if call[0] == operation]) == 1


def test_register_adopts_owned_member_after_lost_create_response(setup):
    sdk, provider, _ = setup
    sdk.rows = [owned_member(tags=[f"lumen-resource:{RESOURCE}", f"lumen-generation:{GENERATION}", "operator"])]
    result = provider.register(RESOURCE, GENERATION, ADDRESS, "member")
    assert result.id == "member" and result.healthy
    assert not any(call[0] in {"create", "update"} for call in sdk.calls)


def test_register_reenables_owned_drained_member(setup):
    sdk, provider, _ = setup
    sdk.rows = [owned_member(weight=0)]
    result = provider.register(RESOURCE, GENERATION, ADDRESS, "member")
    assert result.enabled and result.weight == 1
    assert not any(call[0] == "create" for call in sdk.calls)


@pytest.mark.parametrize("override", [
    {"address": "10.42.0.9"}, {"protocol_port": 8013}, {"subnet_id": "other"},
    {"tags": []}, {"tags": [f"lumen-resource:{RESOURCE}", "lumen-generation:2"]},
    {"tags": ["lumen-resource:other", f"lumen-generation:{GENERATION}"]},
    {"tags": [f"lumen-resource:{RESOURCE}", f"lumen-generation:{GENERATION}", "lumen-generation:2"]},
])
@pytest.mark.parametrize("operation", ["inspect", "register", "drain", "delete"])
def test_name_alone_never_proves_member_ownership(setup, override, operation):
    sdk, provider, _ = setup
    sdk.rows = [owned_member(**override)]
    args = (RESOURCE, GENERATION, ADDRESS)
    if operation in {"drain", "delete"}:
        args = ("member", *args)
    elif operation == "register":
        args = (*args, None)
    with pytest.raises(RuntimeError, match="identity mismatch"):
        getattr(provider, operation)(*args)
    assert not any(call[0] in {"create", "update", "delete"} for call in sdk.calls)


def test_duplicate_name_is_not_adopted(setup):
    sdk, provider, _ = setup
    sdk.rows = [owned_member(), owned_member(id="second")]
    with pytest.raises(RuntimeError, match="duplicate"):
        provider.register(RESOURCE, GENERATION, ADDRESS, None)
    assert not any(call[0] == "create" for call in sdk.calls)


def test_recorded_id_must_match_owned_name(setup):
    sdk, provider, _ = setup
    sdk.rows = [owned_member(weight=0)]
    with pytest.raises(RuntimeError, match="identity mismatch"):
        provider.delete("foreign", RESOURCE, GENERATION, ADDRESS)
    assert not any(call[0] == "delete" for call in sdk.calls)


def test_known_member_missing_from_list_is_drained_and_deleted_by_id(setup):
    sdk, provider, _ = setup
    sdk.rows = [owned_member()]
    original_members = sdk.members

    def hidden_members(pool):
        original_members(pool)
        return []

    sdk.members = hidden_members
    provider.drain("member", RESOURCE, GENERATION, ADDRESS)
    assert sdk.rows[0]["weight"] == 0 and sdk.rows[0]["is_admin_state_up"]
    provider.delete("member", RESOURCE, GENERATION, ADDRESS)
    assert sdk.rows == []
    assert len([call for call in sdk.calls if call[0] == "delete"]) == 1
    assert ("get_member", "member") in sdk.calls


@pytest.mark.parametrize("failure", ["renamed", "failed_read"])
def test_known_member_without_absence_proof_blocks_deletion(setup, failure):
    sdk, provider, _ = setup
    sdk.rows = [owned_member(name="operator-renamed", weight=0)]
    if failure == "failed_read":
        def failed_read(member, pool):
            raise ConnectionError("member read lost")

        sdk.get_member = failed_read
        error, message = ConnectionError, "read lost"
    else:
        error, message = RuntimeError, "identity mismatch"
    with pytest.raises(error, match=message):
        provider.delete("member", RESOURCE, GENERATION, ADDRESS)
    assert not any(call[0] == "delete" for call in sdk.calls)


@pytest.mark.parametrize("hide,fail_reads", [(True, False), (False, True)])
def test_unobservable_create_remains_unknown_and_is_not_retried(setup, hide, fail_reads):
    sdk, provider, _ = setup
    sdk.hide_created = hide
    sdk.fail_reads_after_create = fail_reads
    sdk.lose_response.add("create")
    with pytest.raises(IngressUnknownCreate, match="unknown"):
        provider.register(RESOURCE, GENERATION, ADDRESS, None)
    assert len([call for call in sdk.calls if call[0] == "create"]) == 1


def test_member_disappearance_does_not_skip_pool_active_proof(setup):
    sdk, provider, clock = setup
    sdk.rows = [owned_member(weight=0)]
    original_delete = sdk.delete_member

    def delete_without_visible_member(*args, **kwargs):
        original_delete(*args, **kwargs)
        sdk.rows.clear()
        sdk.pending_action = lambda: None

    sdk.delete_member = delete_without_visible_member
    provider.delete("member", RESOURCE, GENERATION, ADDRESS)
    assert clock.now == 2 and sdk.calls[-1] == ("pool", "ACTIVE")


def test_absence_is_idempotent_only_after_pool_active(setup):
    sdk, provider, clock = setup
    provider.drain(None, RESOURCE, GENERATION, ADDRESS)
    provider.delete(None, RESOURCE, GENERATION, ADDRESS)
    assert clock.now == 0
    sdk.pool_status = "PENDING_UPDATE"
    with pytest.raises(TimeoutError):
        provider.delete(None, RESOURCE, GENERATION, ADDRESS)
    assert clock.now == 3


@pytest.mark.parametrize("status_target", ["member", "pool"])
def test_error_provisioning_fails_closed(setup, status_target):
    sdk, provider, _ = setup
    sdk.rows = [owned_member()]
    if status_target == "member":
        sdk.rows[0]["provisioning_status"] = "ERROR"
    else:
        sdk.pool_status = "ERROR"
    with pytest.raises(RuntimeError, match="provisioning failed"):
        provider.drain("member", RESOURCE, GENERATION, ADDRESS)
    assert not any(call[0] in {"update", "delete"} for call in sdk.calls)


def test_active_status_without_requested_weight_is_not_applied(setup):
    sdk, provider, clock = setup
    sdk.rows = [owned_member()]
    original_update = sdk.update_member

    def accepted_but_not_applied(*args, **kwargs):
        result = original_update(*args, **kwargs)
        sdk.pending_action = lambda: sdk.rows[0].update(provisioning_status="ACTIVE")
        return result

    sdk.update_member = accepted_but_not_applied
    with pytest.raises(TimeoutError):
        provider.drain("member", RESOURCE, GENERATION, ADDRESS)
    assert clock.now == 3 and sdk.rows[0]["weight"] == 1
    assert not any(call[0] == "delete" for call in sdk.calls)


def test_delete_pending_is_not_absence_proof(setup):
    sdk, provider, clock = setup
    sdk.rows = [owned_member(weight=0)]
    sdk.never_active = True
    with pytest.raises(TimeoutError):
        provider.delete("member", RESOURCE, GENERATION, ADDRESS)
    assert clock.now == 3 and sdk.rows[0]["provisioning_status"] == "PENDING_DELETE"
    assert len([call for call in sdk.calls if call[0] == "delete"]) == 1


@pytest.mark.parametrize("key,value", [
    ("timeout_seconds", 0), ("timeout_seconds", float("inf")),
    ("poll_interval_seconds", 0), ("poll_interval_seconds", float("nan")),
])
def test_polling_bounds_are_finite_and_positive(setup, key, value):
    sdk, provider, _ = setup
    with pytest.raises(ValueError, match="finite and positive"):
        IngressProvider(SimpleNamespace(load_balancer=sdk), provider.config, **{key: value})


def test_busy_pool_defers_without_member_create_and_can_be_retried(setup):
    sdk, provider, _ = setup
    sdk.pool_status = "PENDING_UPDATE"
    with pytest.raises(IngressCreateDeferred):
        provider.register(RESOURCE, GENERATION, ADDRESS, None)
    assert not any(call[0] == "create" for call in sdk.calls)
    sdk.pool_status = "ACTIVE"
    assert provider.register(RESOURCE, GENERATION, ADDRESS, None).healthy
    assert len([call for call in sdk.calls if call[0] == "create"]) == 1


def test_observed_member_disappearance_never_creates_without_a_claim(setup):
    sdk, provider, _ = setup
    sdk.rows = [owned_member(weight=0)]
    observed = provider.inspect(RESOURCE, GENERATION, ADDRESS)
    sdk.rows.clear()

    with pytest.raises(RuntimeError, match="refuse unclaimed create"):
        provider.register(RESOURCE, GENERATION, ADDRESS, observed.id)

    assert ("get_member", "member") in sdk.calls
    assert not any(call[0] in {"create", "update"} for call in sdk.calls)


def test_observed_member_uses_direct_id_when_listing_temporarily_omits_it(setup, monkeypatch):
    sdk, provider, _ = setup
    sdk.rows = [owned_member()]
    monkeypatch.setattr(sdk, "members", lambda pool: [])

    member = provider.register(RESOURCE, GENERATION, ADDRESS, "member")

    assert member.id == "member" and member.healthy
    assert not any(call[0] in {"create", "update"} for call in sdk.calls)
