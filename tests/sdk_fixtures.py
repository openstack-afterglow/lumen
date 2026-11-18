"""Shared synthetic model-key boundaries for the official SDK compatibility tests."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from lumen import auth
from lumen.auth import model_key_storage_id
from lumen.models.billing import Payer
from lumen.services import stateless_credit


def model_key_principal(scopes: tuple[str, ...]) -> dict:
    """The post-038 personal model-key principal shape returned by ``verify_key``."""
    return {"auth_type": "api_key", "credential_class": "personal_key", "domain_id": None,
            "payer": Payer("user", "u1", None), "user_id": "u1", "project_id": model_key_storage_id(7),
            "api_key_id": 7, "scopes": scopes, "credential_kind": "api_key", "expires_at": None,
            "source": "api", "roles": [], "is_system_admin": False, "service_system_admin": False}


def personal_key_row(store, monkeypatch) -> None:
    """Persisted personal model-key columns plus a synthetic live Keystone owner/domain."""
    store.key.key_kind, store.key.credential_kind = "personal", "api_key"
    store.key.owner_user_id, store.key.owner_domain_id = "u1", None
    directory = SimpleNamespace(
        users=SimpleNamespace(get=lambda user_id: SimpleNamespace(id=user_id, domain_id="d1", enabled=True)),
        domains=SimpleNamespace(get=lambda domain_id: SimpleNamespace(id=domain_id, enabled=True)),
    )
    monkeypatch.setattr(auth, "_get_admin_ks_client", lambda: directory)


def record_holds(monkeypatch, state) -> None:
    """Payer-scoped stateless holds: record reserve/settle/finish instead of SQL."""
    state.holds = []

    async def reserve(**kwargs):
        state.holds.append(("reserve", kwargs["event_id"], kwargs["payer"]))

    async def settle(**kwargs):
        state.holds.append(("settle", kwargs["event_id"], kwargs["payer"]))
        state.settled.append(kwargs)
        return Decimal("0.000123")

    async def finish(event_id, *, uncertain, payer=None):
        state.holds.append(("finish", event_id, payer))

    monkeypatch.setattr(stateless_credit, "reserve", reserve)
    monkeypatch.setattr(stateless_credit, "settle", settle)
    monkeypatch.setattr(stateless_credit, "finish", finish)
