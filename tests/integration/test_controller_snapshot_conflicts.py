"""Real MariaDB snapshot-conflict regression; only the native cloud effect is fake."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
import test_infrastructure_store as infrastructure_fixtures
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from lumen.db import get_session_factory
from lumen.models.chat_infrastructure import ChatResourceOperation, ChatRuntimePool, ChatRuntimeResource
from lumen.services.infrastructure import store
from lumen.services.infrastructure.controller import ResourceController

pytestmark = pytest.mark.integration

# Reuse both snapshot modes and their isolated pool without re-importing a fixture name.
pool = infrastructure_fixtures.pool
_controller_config = infrastructure_fixtures._controller_config


@pytest.fixture
async def owned_effect(pool):
    """Own one resource, its operation, and its controller; never clean shared rows."""
    factory = get_session_factory()
    controller = ResourceController(_controller_config(), {}, owner="snapshot-" + uuid.uuid4().hex)
    resource = None
    try:
        resource = await store.request_resource(
            pool, role="worker", request_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
            image_ref=pool.image_ref, policy_digest=pool.profile_digest, deadline=None,
        )
        claimed = await store.claim_pool_lease(pool.id, controller.owner, 30)
        assert claimed is not None
        _, fence = claimed
        assert await store.claim_operation(resource.id, resource.generation, "create", controller.owner, fence) is not None
        yield SimpleNamespace(pool=pool, resource=resource, controller=controller, fence=fence, factory=factory)
    finally:
        controller.executor.shutdown(wait=True)
        if resource is not None:
            async with factory() as session, session.begin():
                await session.execute(delete(ChatResourceOperation).where(ChatResourceOperation.resource_id == resource.id))
                await session.execute(delete(ChatRuntimeResource).where(
                    ChatRuntimeResource.id == resource.id, ChatRuntimeResource.pool_id == pool.id,
                ))


async def test_fenced_cloud_reads_committed_before_concurrent_pool_renewal(owned_effect, monkeypatch):
    """Commit a real pool renewal after the resource read but before its pool lock."""
    f = owned_effect
    resource_read = asyncio.Event()
    renewal_committed = asyncio.Event()
    events = []
    calls = []
    isolation_levels = []
    effect_session = None
    original_execute = AsyncSession.execute
    resource_lookup = select(ChatRuntimeResource.pool_id).where(ChatRuntimeResource.id == f.resource.id)
    pool_lock = select(ChatRuntimePool).where(ChatRuntimePool.id == f.pool.id).with_for_update()
    resource_sql = str(resource_lookup.compile(compile_kwargs={"literal_binds": True}))
    pool_sql = str(pool_lock.compile(compile_kwargs={"literal_binds": True}))

    async def execute(session, statement, *args, **kwargs):
        nonlocal effect_session
        sql = str(statement.compile(compile_kwargs={"literal_binds": True})) if isinstance(statement, Select) else None
        if effect_session is None and sql == resource_sql:
            effect_session = session
            # The consistent read must really finish before the other transaction writes.
            result = await original_execute(session, statement, *args, **kwargs)
            connection = await session.connection()
            isolation_levels.append(await connection.get_isolation_level())
            assert isolation_levels == ["READ COMMITTED"]
            events.append("resource-read")
            resource_read.set()
            await asyncio.wait_for(renewal_committed.wait(), 5)
            return result
        if session is effect_session and sql == pool_sql:
            assert renewal_committed.is_set()
            events.append("pool-lock")
        return await original_execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", execute)

    async def renew():
        await asyncio.wait_for(resource_read.wait(), 5)
        # No mocked store/lease result: this locks, updates, and commits the actual pool row.
        assert await store.renew_pool_lease(f.pool.id, f.controller.owner, f.fence, 300)
        async with f.factory() as session:
            renewed = await session.get(ChatRuntimePool, f.pool.id)
            assert renewed.reconcile_lease_owner == f.controller.owner
            assert renewed.reconcile_fence == f.fence
            assert (store._utc(renewed.reconcile_lease_expires_at) - store._now()).total_seconds() > 200
        events.append("renewal-committed")
        renewal_committed.set()

    def native_effect(resource_id):
        calls.append(resource_id)
        events.append("native")
        return "synthetic-provider-accepted"

    renewal = asyncio.create_task(renew())
    try:
        result = await asyncio.wait_for(f.controller._claimed_effect(
            f.resource, f.fence, "create", native_effect, f.resource.id,
        ), 10)
        await asyncio.wait_for(renewal, 5)
        assert result == "synthetic-provider-accepted"
        assert calls == [f.resource.id]
        assert events == ["resource-read", "renewal-committed", "pool-lock", "native"]
        assert isolation_levels == ["READ COMMITTED"]
        async with f.factory() as session:
            operation = await session.scalar(select(ChatResourceOperation).where(
                ChatResourceOperation.resource_id == f.resource.id,
                ChatResourceOperation.generation == f.resource.generation,
                ChatResourceOperation.action == "create",
            ))
            assert (operation.request_status, operation.owner, operation.fence, operation.attempts) == (
                "claimed", f.controller.owner, f.fence, 1,
            )
    finally:
        if not renewal.done():
            renewal.cancel()
        await asyncio.gather(renewal, return_exceptions=True)
