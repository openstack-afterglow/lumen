"""A new user's concurrent first requests must all see one wallet row instead of failing.

``UserWallet`` rows are created lazily. The read-then-insert used to let racing first requests insert the
same primary key, so all but one failed with a duplicate-key error surfaced as ``ChatStorageUnavailable``.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, event, func, select, text

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_db import UserWallet
from lumen.services import credit

pytestmark = [pytest.mark.integration]

_REQUESTS = 12


@pytest.mark.parametrize("path", ["precheck", "reserve"])
@pytest.mark.parametrize("snapshot_isolation", ["OFF", "ON"])
async def test_concurrent_first_requests_create_one_wallet(request, snapshot_isolation, path):
    from sqlalchemy.pool import Pool

    def pin_isolation(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET SESSION innodb_snapshot_isolation={snapshot_isolation}")
        cursor.close()

    event.listen(Pool, "connect", pin_isolation)
    request.addfinalizer(lambda: event.remove(Pool, "connect", pin_isolation))
    init_db(os.environ["DATABASE_URL"], pool_size=_REQUESTS + 2, max_overflow=0)
    factory = get_session_factory()
    assert factory is not None
    user_id, project_id = f"wallet-user-{uuid.uuid4().hex}", "wallet-project"

    async def precheck():
        await credit.precheck(user_id, project_id)

    async def reserve():
        # The media/realtime provider-call start: READ COMMITTED set before the first statement.
        async with factory() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            await credit.reserve_call_credit_in_transaction(
                session, user_id=user_id, project_id=project_id, api_key_id=None, bound=Decimal("0"))

    try:
        await asyncio.gather(*((precheck if path == "precheck" else reserve)() for _ in range(_REQUESTS)))
        async with factory() as session:
            wallets = await session.scalar(select(func.count()).select_from(UserWallet).where(
                UserWallet.user_id == user_id))
        assert wallets == 1
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(UserWallet).where(UserWallet.user_id == user_id))
        await close_db()
