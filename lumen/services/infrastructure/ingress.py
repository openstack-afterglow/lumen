"""Operator-owned Octavia pool members; no load balancer creation or user input."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from openstack.exceptions import ResourceNotFound

from lumen.services.infrastructure.config import IngressConfig


@dataclass(frozen=True)
class MemberState:
    id: str
    healthy: bool
    enabled: bool
    weight: int
    provisioning_status: str


class IngressCreateDeferred(RuntimeError):
    """The shared pool is busy; no member-create request was submitted."""


class IngressUnknownCreate(RuntimeError):
    """A create may have committed; only inspection may resolve it, not another create."""


class IngressProvider:
    def __init__(self, connection: object, config: IngressConfig, *,
                 timeout_seconds: float = 30, poll_interval_seconds: float = 1):
        if (not math.isfinite(timeout_seconds) or timeout_seconds <= 0
                or not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0):
            raise ValueError("ingress polling bounds must be finite and positive")
        self.proxy = connection.load_balancer
        self.config = config
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds

    @staticmethod
    def _tags(resource_id: str, generation: int) -> list[str]:
        return [f"lumen-resource:{resource_id}", f"lumen-generation:{generation}"]

    def _member(self, resource_id: str, generation: int, address: str,
                member_id: str | None = None):
        matches = [
            member for member in self.proxy.members(self.config.ingress_pool_id)
            if member.name == f"lumen-{resource_id}"
        ]
        if len(matches) > 1:
            raise RuntimeError("duplicate ingress members require operator reconciliation")
        if member_id is not None:
            try:
                member = self.proxy.get_member(member_id, self.config.ingress_pool_id)
            except ResourceNotFound:
                if matches:
                    raise RuntimeError("existing ingress member identity mismatch") from None
                return None
            if matches and matches[0].id != member.id:
                raise RuntimeError("existing ingress member identity mismatch")
        else:
            if not matches:
                return None
            member = matches[0]
        tags = {tag for tag in (member.tags or [])
                if tag.startswith(("lumen-resource:", "lumen-generation:"))}
        if (tags != set(self._tags(resource_id, generation))
                or member.name != f"lumen-{resource_id}"
                or member.address != address
                or member.protocol_port != self.config.ingress_member_port
                or member.subnet_id != self.config.ingress_subnet_id
                or (member_id is not None and member.id != member_id)):
            raise RuntimeError("existing ingress member identity mismatch")
        return member

    def _pool_active(self) -> bool:
        pool = self.proxy.get_pool(self.config.ingress_pool_id)
        if pool.provisioning_status == "ERROR":
            raise RuntimeError("ingress pool provisioning failed")
        return pool.provisioning_status == "ACTIVE"

    @staticmethod
    def _state(member, pool_active: bool) -> MemberState:
        active = member.provisioning_status == "ACTIVE" and pool_active
        return MemberState(
            member.id, active and member.operating_status == "ONLINE",
            bool(member.is_admin_state_up) and member.weight > 0,
            member.weight, member.provisioning_status,
        )

    def inspect(self, resource_id: str, generation: int, address: str) -> MemberState | None:
        """Read-only ownership lookup; a pending member is never healthy."""
        member = self._member(resource_id, generation, address)
        return None if member is None else self._state(member, self._pool_active())

    def _wait(self, resource_id: str, generation: int, address: str, *,
              member_id: str | None = None, weight: int | None = None,
              absent: bool = False):
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            member = self._member(resource_id, generation, address, member_id)
            pool_active = self._pool_active()
            if member is not None and member.provisioning_status == "ERROR":
                raise RuntimeError("ingress member provisioning failed")
            if absent:
                if member is None and pool_active:
                    return None
            elif (member is not None and pool_active
                  and member.provisioning_status == "ACTIVE"
                  and (weight is None or (member.weight == weight and member.is_admin_state_up))):
                return member
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("ingress provisioning did not become ACTIVE before deadline")
            time.sleep(min(self.poll_interval_seconds, remaining))

    def register(self, resource_id: str, generation: int, address: str, member_id: str | None) -> MemberState:
        """Create/adopt with weight=1; caller holds the pool fence until polling settles.

        Never retry a write after a lost SDK response. Observe its exact identity and
        applied state instead; ambiguous creates remain unknown to the controller.
        """
        member = self._member(resource_id, generation, address, member_id)
        if member is None:
            if member_id is not None:
                raise RuntimeError("observed ingress member disappeared; refuse unclaimed create")
            # Other pending writes on the operator pool must settle before creation.
            if not self._pool_active():
                raise IngressCreateDeferred("ingress pool is not ACTIVE; create deferred")
            try:
                self.proxy.create_member(
                    self.config.ingress_pool_id, name=f"lumen-{resource_id}", address=address,
                    protocol_port=self.config.ingress_member_port,
                    subnet_id=self.config.ingress_subnet_id, is_admin_state_up=True, weight=1,
                    tags=self._tags(resource_id, generation),
                )
            except Exception:
                # The request may already have committed. No second create is safe.
                pass
            try:
                member = self._wait(resource_id, generation, address, weight=1)
            except Exception as exc:
                raise IngressUnknownCreate("ingress create outcome unknown; inspect before any new write") from exc
        else:
            member = self._wait(resource_id, generation, address, member_id=member.id)
            if not member.is_admin_state_up or member.weight != 1:
                try:
                    self.proxy.update_member(member.id, self.config.ingress_pool_id,
                                             is_admin_state_up=True, weight=1)
                except Exception:
                    # Polling proves application or fails closed, including lost responses.
                    pass
                member = self._wait(resource_id, generation, address, member_id=member.id, weight=1)
        return self._state(member, True)

    def drain(self, member_id: str | None, resource_id: str, generation: int, address: str) -> None:
        """Withdraw new traffic, preserving established connections and admin-up state."""
        member = self._member(resource_id, generation, address, member_id)
        if member is None:
            self._wait(resource_id, generation, address, absent=True)
            return
        member = self._wait(resource_id, generation, address, member_id=member.id)
        if member.weight != 0 or not member.is_admin_state_up:
            try:
                self.proxy.update_member(member.id, self.config.ingress_pool_id,
                                         weight=0, is_admin_state_up=True)
            except Exception:
                pass
        self._wait(resource_id, generation, address, member_id=member.id, weight=0)

    def delete(self, member_id: str | None, resource_id: str, generation: int, address: str) -> None:
        """Remove only the owned, applied drained member; prove absence before return."""
        member = self._member(resource_id, generation, address, member_id)
        if member is not None:
            member = self._wait(resource_id, generation, address, member_id=member.id, weight=0)
            try:
                self.proxy.delete_member(member.id, self.config.ingress_pool_id, ignore_missing=True)
            except Exception:
                pass
        self._wait(resource_id, generation, address, member_id=member_id, absent=True)
