"""Combined migrations 037/038: real credit admission for persisted model keys."""

import os
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete

from lumen import auth
from lumen.db import close_db, get_session_factory, init_db
from lumen.models.billing import Payer
from lumen.models.chat_db import ChatApiKey, UserWallet
from lumen.models.stateless_reservations import StatelessCallReservation
from lumen.scripts.migrate import load_manifest
from lumen.services import api_key_store

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("kind", ["personal", "service"])
async def test_persisted_model_keys_pass_real_credit_precheck_and_reservation(monkeypatch, kind):
    if not any(item.logical_id == "037-domain-credit-pools" for item in load_manifest()):
        pytest.skip("combined key and billing schema requires migration 037")
    from lumen.models.billing import DomainCreditLedger, DomainCreditPool
    from lumen.services import credit, domain_credit, stateless_credit

    domain_id, owner_id, event_id = (uuid4().hex for _ in range(3))
    key_id = None
    init_db(os.environ["DATABASE_URL"], pool_size=2, max_overflow=0)
    factory = get_session_factory()
    assert factory is not None

    def user(user_id):
        assert kind == "personal" and user_id == owner_id
        return SimpleNamespace(id=user_id, domain_id=domain_id, enabled=True)

    def domain(identity):
        assert identity == domain_id
        return SimpleNamespace(id=identity, enabled=True)

    monkeypatch.setattr(auth, "_get_admin_ks_client", lambda: SimpleNamespace(
        users=SimpleNamespace(get=user), domains=SimpleNamespace(get=domain),
    ))

    try:
        ownership = {"owner_user_id": owner_id} if kind == "personal" else {"owner_domain_id": domain_id}
        row, raw = api_key_store._new_key(
            "billing fixture", ("compat:completions:write",), expires_at=None, key_kind=kind,
            credential_kind="api_key", created_by_user_id=owner_id, **ownership,
        )
        async with factory() as session, session.begin():
            session.add(DomainCreditPool(balance_credits=Decimal(10), domain_id=domain_id,
                                         period_start=domain_credit.current_period()))
            row.owner_monthly_credit_limit = Decimal(5)
            session.add(row)
            await session.flush()
            key_id = row.id

        principal = await api_key_store.verify_key(raw)
        expected_payer = Payer("user", owner_id, None) if kind == "personal" else Payer("domain", None, domain_id)
        assert principal is not None and principal["payer"] == expected_payer
        actor = {name: principal[name] for name in ("user_id", "project_id", "api_key_id", "payer")}
        await credit.precheck(**actor)
        await stateless_credit.reserve(**actor, event_id=event_id, bound=Decimal(4))

        async with factory() as session:
            hold = await session.get(StatelessCallReservation, event_id)
            assert hold.status == "reserved" and hold.bound_credits == 4
            assert hold.api_key_id == key_id and hold.project_id is None
            assert (hold.payer_user_id, hold.payer_domain_id) == (expected_payer.user_id, expected_payer.domain_id)
            pool = await session.get(DomainCreditPool, domain_id)
            if kind == "service":
                assert hold.user_id is None and pool.reserved_credits == 4
                assert await session.get(UserWallet, auth.model_key_storage_id(key_id)) is None
            else:
                assert hold.user_id == owner_id and pool.reserved_credits == 0
        with pytest.raises(credit.QuotaExceeded, match="API 키 월 사용 한도"):
            await credit.precheck(**actor, bound=Decimal(2))
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(StatelessCallReservation).where(StatelessCallReservation.event_id == event_id))
            if key_id is not None:
                await session.execute(delete(ChatApiKey).where(ChatApiKey.id == key_id))
            await session.execute(delete(DomainCreditLedger).where(DomainCreditLedger.domain_id == domain_id))
            await session.execute(delete(DomainCreditPool).where(DomainCreditPool.domain_id == domain_id))
            await session.execute(delete(UserWallet).where(UserWallet.user_id == owner_id))
        await close_db()
