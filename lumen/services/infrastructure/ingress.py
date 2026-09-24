"""Operator-owned Octavia pool members; no load balancer creation or user input."""

from __future__ import annotations

from dataclasses import dataclass

from lumen.services.infrastructure.config import IngressConfig


@dataclass(frozen=True)
class MemberState:
    id: str
    healthy: bool
    enabled: bool


class IngressProvider:
    def __init__(self, connection: object, config: IngressConfig):
        self.proxy = connection.load_balancer
        self.config = config

    def _member(self, resource_id: str, address: str):
        matches = [
            member for member in self.proxy.members(self.config.ingress_pool_id)
            if member.name == f"lumen-{resource_id}"
        ]
        if len(matches) > 1:
            raise RuntimeError("duplicate ingress members require operator reconciliation")
        if not matches:
            return None
        member = matches[0]
        if (member.address != address or member.protocol_port != self.config.ingress_member_port
                or member.subnet_id != self.config.ingress_subnet_id):
            raise RuntimeError("existing ingress member identity mismatch")
        return member

    @staticmethod
    def _state(member) -> MemberState:
        return MemberState(member.id, member.operating_status == "ONLINE", bool(member.is_admin_state_up))

    def inspect(self, resource_id: str, address: str) -> MemberState | None:
        """Read-only lookup; safe without the pool fence because it never mutates Octavia."""
        member = self._member(resource_id, address)
        return None if member is None else self._state(member)

    def register(self, resource_id: str, address: str) -> MemberState:
        """Create or re-enable a member; the caller must hold the pool fence until this returns."""
        member = self._member(resource_id, address)
        if member is None:
            member = self.proxy.create_member(
                self.config.ingress_pool_id, name=f"lumen-{resource_id}", address=address,
                protocol_port=self.config.ingress_member_port,
                subnet_id=self.config.ingress_subnet_id, is_admin_state_up=True,
            )
        elif not member.is_admin_state_up:
            member = self.proxy.update_member(member.id, self.config.ingress_pool_id, is_admin_state_up=True)
        return self._state(member)

    def drain(self, member_id: str) -> None:
        member = self.proxy.find_member(member_id, self.config.ingress_pool_id)
        if member and member.is_admin_state_up:
            self.proxy.update_member(member_id, self.config.ingress_pool_id, is_admin_state_up=False)

    def delete(self, member_id: str) -> None:
        self.proxy.delete_member(member_id, self.config.ingress_pool_id, ignore_missing=True)
