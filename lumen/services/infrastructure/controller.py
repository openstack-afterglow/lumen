"""Fenced, restart-safe resource reconciliation against durable intent.

External effects are never fenced by a DB commit alone: a create is claimed exactly
once per generation before any OpenStack call, an unknown result is quarantined
until an owned resource is found by identity, and delete recheck ownership
immediately before I/O. Octavia member create/enable holds the pool fence row
lock across the SDK call, so a lease takeover can only drain after it. Readiness
requires bootstrap-issued identity plus either a
worker/plugin registration or an authenticated ``/readyz`` probe; cloud "ACTIVE"
alone is never sufficient.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from lumen.db import get_session_factory
from lumen.models.chat_infrastructure import ChatRuntimePool, ChatRuntimeResource
from lumen.services.durable_runs import lifecycle
from lumen.services.infrastructure import bootstrap, store
from lumen.services.infrastructure.config import PoolConfig, RuntimeConfig
from lumen.services.infrastructure.guest_api import api_load
from lumen.services.infrastructure.identity import accepted_fingerprints
from lumen.services.infrastructure.ingress import IngressCreateDeferred, IngressProvider
from lumen.services.infrastructure.providers import Observation, ResourceIntent, ResourceRef
from lumen.services.infrastructure.scheduler import api_desired, capacity_limit, gate_scale, worker_desired
from lumen.services.infrastructure.transport import InternalTransport, InternalTransportError

logger = logging.getLogger(__name__)

# Cloud states that only ever mean "the provider accepted the request"; never
# ready, and only "booting" once ownership/identity has already been verified.
_LIVE_STATES = {"active", "running", "ready", "booting", "build", "creating", "created"}
_DEAD_STATES = {"error", "failed"}


class PoolLeaseLost(RuntimeError):
    pass


_LEASE = contextvars.ContextVar("runtime_pool_lease", default=None)


class ResourceController:
    def __init__(self, config: RuntimeConfig, providers: dict, *, owner: str):
        self.config = config
        self.providers = providers
        self.ingresses = {
            pool.name: IngressProvider(providers[pool.name].connection, pool.ingress)
            for pool in config.pools
            if pool.enabled and pool.role == "api" and pool.ingress is not None and pool.name in providers
        }
        self.owner = owner
        self.executor = ThreadPoolExecutor(max_workers=min(config.max_parallel_cloud_operations, 4))
        self.limiter = asyncio.Semaphore(min(config.max_parallel_cloud_operations, 4))
        needs_probe = any(pool.enabled and pool.role in {"api", "sandbox"} for pool in config.pools)
        self.transport = InternalTransport(config) if needs_probe else None
        self._pool_ids: dict[str, str] = {}
        self._stopping = asyncio.Event()
        self._retry: dict[str, tuple[float, int]] = {}
        self._api_load: dict[str, tuple[float, int, int | None]] = {}

    def stop(self) -> None:
        self._stopping.set()

    async def _cloud(self, function, *args):
        """Read I/O checks the current lease after waiting for the shared limiter."""
        async with self.limiter:
            lease = _LEASE.get()
            if lease is not None and not await store.pool_lease_owned(lease[0], self.owner, lease[1]):
                raise PoolLeaseLost()
            return await self._settled(function, *args)

    async def _fenced_cloud(self, resource, fence: int, function, *args, action: str | None = None):
        """Order non-conditional cloud writes with takeover using the pool row lock."""
        factory = get_session_factory()
        async with self.limiter, factory() as session, session.begin():
            row, lease = await store._locked_resource(session, resource.id, resource.generation)
            if row is None or not store._owns(lease, self.owner, fence):
                raise PoolLeaseLost()
            if action == "create" and row.desired_state != "requested":
                raise PoolLeaseLost()
            if action == "delete":
                if row.desired_state != "deleting":
                    raise PoolLeaseLost()
                if (row.role == "worker" and (row.ready_at is not None or row.drain_requested_at is not None
                        or row.observed_state in {"ready", "draining"}) and row.drain_ack_at is None
                        and row.failure_code != "resource_vanished"):
                    raise store.ResourceUnavailable("drain_pending")
            return await self._renewed_effect(lease, function, *args)

    async def _renewed_effect(self, lease, function, *args):
        # The row lock orders takeover after this write. Renew the locked row while
        # SDK polling settles; the transaction publishes the final lease deadline.
        done = asyncio.Event()
        async def renew_locked():
            while not done.is_set():
                lease.reconcile_lease_expires_at = store._now() + timedelta(seconds=30)
                try:
                    await asyncio.wait_for(done.wait(), 10)
                except TimeoutError:
                    pass
        renewal = asyncio.create_task(renew_locked())
        try:
            return await self._settled(function, *args)
        finally:
            done.set()
            await renewal

    def _backoff(self, key: str) -> None:
        loop = asyncio.get_running_loop()
        attempt = self._retry.get(key, (0, 0))[1] + 1
        self._retry[key] = (loop.time() + min(60, 2 ** min(attempt, 6)), attempt)

    def _clear_backoff(self, key: str) -> None:
        self._retry.pop(key, None)

    def _due(self, key: str) -> bool:
        return self._retry.get(key, (0, 0))[0] <= asyncio.get_running_loop().time()

    async def run(self) -> None:
        try:
            self._pool_ids = {row.name: row.id for row in await store.sync_pools(self.config)}
            while not self._stopping.is_set():
                try:
                    await self.tick()
                except Exception:
                    logger.exception("resource controller tick failed")
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.config.reconcile_interval_seconds)
                except TimeoutError:
                    pass
        finally:
            # Never cancel an in-flight cloud thread: an ambiguous create must
            # remain durable intent for the next leader, not a lost future.
            self.executor.shutdown(wait=True)

    async def tick(self) -> None:
        await asyncio.gather(*(self._tick_pool(definition) for definition in self.config.pools
            if definition.enabled and definition.name in self.providers and definition.name in self._pool_ids))

    async def _tick_pool(self, definition: PoolConfig) -> None:
        pool_id = self._pool_ids[definition.name]
        claim = await store.claim_pool_lease(pool_id, self.owner, 30)
        if claim is None:
            return
        pool, fence = claim
        done = asyncio.Event()
        async def renew():
            while not done.is_set():
                try:
                    await asyncio.wait_for(done.wait(), 10)
                except TimeoutError:
                    if not await store.renew_pool_lease(pool.id, self.owner, fence, 30):
                        return
        renewal = asyncio.create_task(renew())
        token = _LEASE.set((pool.id, fence))
        try:
            await self.reconcile_pool(pool, fence, definition)
        except PoolLeaseLost:
            logger.info("pool %s reconcile lease lost", pool.name)
        except Exception:
            logger.exception("pool %s reconcile failed", pool.name)
        finally:
            _LEASE.reset(token)
            done.set()
            await renewal

    async def _resources(self, pool_id: str) -> list[ChatRuntimeResource]:
        factory = get_session_factory()
        if factory is None:
            raise RuntimeError("database is not configured")
        async with factory() as session:
            return list((await session.execute(select(ChatRuntimeResource).where(
                ChatRuntimeResource.pool_id == pool_id, ChatRuntimeResource.observed_state != "deleted"
            ))).scalars())

    def _intent(self, resource: ChatRuntimeResource, definition: PoolConfig,
                *, bootstrap_token: str | None = None) -> ResourceIntent:
        return ResourceIntent(
            resource_id=resource.id, generation=resource.generation, pool_id=definition.name,
            role=resource.role, image_ref=resource.image_ref, policy_digest=resource.policy_digest,
            request_fingerprint=resource.request_fingerprint, deadline=resource.deadline_at,
            run_id=resource.run_id, logical_project_id=resource.logical_project_id,
            logical_user_id=resource.logical_user_id, bootstrap_token=bootstrap_token,
            guest_profile_id=getattr(resource, "guest_profile_id", None),
            guest_profile_digest=getattr(resource, "guest_profile_digest", None),
        )

    def _ref(self, resource: ChatRuntimeResource, definition: PoolConfig) -> ResourceRef | None:
        if not resource.provider_id:
            return None
        return ResourceRef(resource.id, resource.generation, resource.provider_id, definition.name)

    async def reconcile_pool(self, pool: ChatRuntimePool, fence: int, definition: PoolConfig) -> None:
        provider = self.providers[definition.name]
        resources = await self._resources(pool.id)
        # Listed ownership is the only evidence for an unknown create; absence in an
        # eventually-consistent list is never proof that the create did not happen.
        needs_listing = any(r.observed_state == "unknown" and not r.provider_id for r in resources)
        owned: list[Observation] | None = None
        if needs_listing and self._due(f"list:{pool.id}"):
            try:
                owned = await self._cloud(provider.list_owned, definition.name)
                self._clear_backoff(f"list:{pool.id}")
            except Exception:
                logger.exception("owned resource scan failed for pool %s", pool.name)
                self._backoff(f"list:{pool.id}")
        await asyncio.gather(*(
            self._reconcile_resource(resource, pool, definition, fence, provider, owned) for resource in resources
        ))
        resources = await self._resources(pool.id)
        await asyncio.gather(*(
            self._advance_readiness(resource, pool, fence, definition) for resource in resources
            if resource.desired_state != "deleting"
        ))
        resources = await self._resources(pool.id)
        if definition.role == "sandbox":
            await self._fail_expired_waiters(resources)
            await store.write_projection(pool.id, self.owner, fence, desired=len(resources),
                ready=sum(r.observed_state == "ready" for r in resources),
                provisioning=sum(r.observed_state in {"requested", "creating", "booting"} for r in resources),
                draining=sum(r.desired_state == "deleting" for r in resources), queued=0, oldest=None,
                reason="unknown_create" if any(r.observed_state == "unknown" for r in resources) else None,
                high_samples=0, low_since=None)
            return
        if definition.role == "worker":
            await store.list_stale_workers()
            await store.update_worker_idle(pool.id, self.owner, fence)
        resources = await self._resources(pool.id)
        try:
            retirement_reason = await self._retire_expired(resources, fence, definition)
        except PoolLeaseLost:
            raise
        except Exception:
            logger.exception("pool %s replacement not applied", pool.name)
            retirement_reason = "cloud_quota"
        resources = await self._resources(pool.id)
        for resource in resources:
            if resource.drain_requested_at is not None and resource.desired_state != "deleting":
                try:
                    await self._advance_drain(resource, fence, definition)
                except PoolLeaseLost:
                    raise
                except Exception:
                    logger.exception("resource %s drain not applied", resource.id)
                    retirement_reason = "drain_pending"
        await self._scale(pool, fence, definition, await self._resources(pool.id), retirement_reason=retirement_reason)

    async def _advance_readiness(self, resource: ChatRuntimeResource, pool: ChatRuntimePool,
                                  fence: int, definition: PoolConfig) -> None:
        if resource.observed_state == "draining" and resource.role == "api":
            await self._probe_api_load(resource, fence, definition)
            return
        if resource.observed_state == "ready":
            if resource.role == "sandbox":
                await store.wake_ready_runs(resource.id)
            elif (resource.role == "api" and resource.address and definition.name in self.ingresses
                  and resource.drain_requested_at is None
                  and await self._probe_api_load(resource, fence, definition)):
                await self._ensure_ingress(resource, pool, fence, definition)
            return
        if (resource.observed_state not in {"booting", "unavailable"} or not resource.certificate_fingerprint
                or resource.drain_requested_at is not None
                or (resource.observed_state == "unavailable" and resource.role != "api")):
            return
        if resource.role == "api":
            if (resource.address and await self._probe_api_load(resource, fence, definition)
                    and definition.name in self.ingresses):
                await self._ensure_ingress(resource, pool, fence, definition)
            return
        if resource.role == "worker":
            # Readiness for trusted workers is entirely driven by the worker's own
            # registration/heartbeat calls; the controller only re-tests the gate.
            await store.mark_ready(resource.id, resource.generation, self.owner, fence)
            return
        if self.transport is None or not resource.address:
            return
        key = f"readyz:{resource.id}"
        if not self._due(key):
            return
        try:
            await self.transport.request(
                address=resource.address, port=resource.port or self.config.listen_port,
                role=resource.role, resource_id=resource.id, generation=resource.generation,
                method="GET", certificate_fingerprints=frozenset(accepted_fingerprints(resource)),
                path="/readyz",
                deadline=datetime.now(UTC) + timedelta(seconds=10),
            )
        except InternalTransportError:
            self._backoff(key)
            return
        self._clear_backoff(key)
        await store.mark_ready(resource.id, resource.generation, self.owner, fence, policy_verified=True)

    async def _probe_api_load(self, resource: ChatRuntimeResource, fence: int, definition: PoolConfig) -> bool:
        if self.transport is None or not resource.address:
            return False
        key = f"readyz:{resource.id}"
        if not self._due(key):
            return False
        try:
            response = await self.transport.request(
                address=resource.address, port=resource.port or self.config.listen_port,
                role="api", resource_id=resource.id, generation=resource.generation,
                method="GET", certificate_fingerprints=frozenset(accepted_fingerprints(resource)),
                path="/v1/ready", deadline=datetime.now(UTC) + timedelta(seconds=10),
            )
            envelope = json.loads(response)
            if (not isinstance(envelope, dict) or type(envelope.get("ready")) is not bool
                    or type(envelope.get("draining")) is not bool or not isinstance(envelope.get("load"), dict)):
                raise InternalTransportError("API readiness metrics unavailable")
            metrics = api_load(json.dumps(envelope["load"]).encode())
            if metrics is None:
                raise InternalTransportError("API readiness metrics unavailable")
            active, p95 = metrics
            active += envelope["load"]["active_ws"]
            if not await store.record_api_load(resource.id, resource.generation, self.owner, fence,
                                               active_count=active, snapshot=envelope["load"],
                                               drain_acknowledged=(envelope.get("draining") is True
                                                   and envelope.get("drain_acknowledged") is True),
                                               drain_fence=envelope.get("drain_fence")):
                return False
            resource.active_slots = active
            if envelope["draining"]:
                self._api_load.pop(resource.id, None)
                return (resource.drain_requested_at is not None and active == 0
                        and envelope.get("drain_acknowledged") is True
                        and type(envelope.get("drain_fence")) is int and envelope["drain_fence"] == fence)
            if not envelope["ready"]:
                raise InternalTransportError("API dependencies unavailable")
            observed_age = (datetime.now(UTC) - datetime.fromisoformat(envelope["load"]["observed_at"])).total_seconds()
            self._api_load[resource.id] = (time.monotonic() - observed_age, active, p95)
            self._clear_backoff(key)
            return True
        except (InternalTransportError, ValueError, TypeError):
            self._api_load.pop(resource.id, None)
            self._backoff(key)
            if resource.observed_state == "ready":
                if resource.ingress_member_id:
                    # Retain the resource if Octavia cannot confirm a disabled
                    # member; never re-enable an unverified process.
                    ingress = self.ingresses.get(definition.name)
                    if ingress is not None:
                        try:
                            await self._fenced_cloud(resource, fence, ingress.drain,
                                resource.ingress_member_id, resource.id, resource.generation, resource.address)
                        except Exception:
                            logger.exception("API ingress withdrawal failed for resource %s", resource.id)
                await store.mark_api_unavailable(resource.id, resource.generation, self.owner, fence)
                resource.observed_state = "unavailable"
            return False

    async def _ensure_ingress(self, resource: ChatRuntimeResource, pool: ChatRuntimePool,
                              fence: int, definition: PoolConfig) -> None:
        """Create/enable an API member only while the pool fence is held across the write.

        Octavia has no conditional update, so check-then-write would let a controller that
        lost its lease re-enable a member the new owner already drained. Lease claims and
        every fenced resource transition lock the pool row first; holding that lock from
        the ownership check until the SDK call settles orders any takeover, deletion
        intent, and the new owner's drain strictly after this write.
        """
        ingress = self.ingresses[definition.name]
        try:
            current = await self._cloud(ingress.inspect, resource.id, resource.generation, resource.address)
        except Exception:
            logger.exception("ingress lookup failed for resource %s", resource.id)
            await store.mark_api_unavailable(resource.id, resource.generation, self.owner, fence)
            resource.observed_state = "unavailable"
            return
        if (current is not None and current.healthy and current.enabled
                and current.id == resource.ingress_member_id):
            if await store.mark_ready(resource.id, resource.generation, self.owner, fence, policy_verified=True):
                resource.observed_state = "ready"
            return
        if current is None and not await store.claim_ingress_create(resource.id, resource.generation, self.owner, fence):
            return
        factory = get_session_factory()
        if factory is None:
            raise RuntimeError("database is not configured")
        try:
            # Limiter first: a coroutine holding the pool row lock never waits on the limiter.
            async with self.limiter, factory() as session, session.begin():
                row, lease = await store._locked_resource(session, resource.id, resource.generation)
                if (row is None or row.pool_id != pool.id or not store._owns(lease, self.owner, fence)
                        or row.drain_requested_at is not None or not row.accepting
                        or row.role != "api" or row.observed_state not in {"ready", "booting", "unavailable"}
                        or row.desired_state == "deleting" or row.bootstrap_token_hash is not None
                        or not accepted_fingerprints(row)
                        or row.address != resource.address):
                    if current is None:
                        # This call committed the claim above but never reached Octavia.
                        raise IngressCreateDeferred("ingress create precondition changed")
                    return
                member = await self._renewed_effect(
                    lease, ingress.register, row.id, row.generation, row.address,
                    current.id if current is not None else None,
                )
                if row.ingress_member_id != member.id:
                    row.ingress_member_id = member.id
                ready = member.healthy and await store._mark_ready_locked(
                    session, row, lease, self.owner, fence, policy_verified=True)
                if not ready and row.observed_state == "ready":
                    row.observed_state = "unavailable"
        except IngressCreateDeferred:
            # Unlike a lost create response, no member write was submitted; release
            # this call's claim so a later fenced tick may create or withdraw.
            await store.defer_unsubmitted_ingress_create(resource.id, resource.generation, self.owner, fence)
            return
        except Exception:
            logger.exception("ingress registration failed for resource %s", resource.id)
            await store.mark_api_unavailable(resource.id, resource.generation, self.owner, fence)
            resource.observed_state = "unavailable"
            return
        # Same-tick scale-in/retirement must drain the member just enabled.
        resource.ingress_member_id = member.id
        await store.record_ingress_member(resource.id, resource.generation, self.owner, fence, member.id)
        if ready:
            resource.observed_state = "ready"
        elif resource.observed_state == "ready":
            resource.observed_state = "unavailable"

    async def _settled(self, function, *args):
        """Run an SDK write and never return before its thread finishes.

        The executor thread cannot be cancelled; releasing the caller's DB fence on task
        cancellation would let the write land after a lease takeover.
        """
        future = asyncio.get_running_loop().run_in_executor(self.executor, lambda: function(*args))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            while not future.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait({future})
            if not future.cancelled():
                future.exception()
            raise

    @staticmethod
    def _retirement_due(resource: ChatRuntimeResource, definition: PoolConfig, *,
                        now: datetime | None = None) -> bool:
        created = store._utc(resource.created_at)
        return ((now or datetime.now(UTC)) - created).total_seconds() >= (
            definition.max_lifetime_seconds - definition.boot_timeout_seconds - definition.drain_seconds)

    @staticmethod
    def _physical_limit(definition: PoolConfig) -> int:
        return capacity_limit(maximum=definition.max_replicas + definition.max_surge,
            db_budget=definition.db_connection_budget, db_per_process=definition.db_connections_per_process,
            pg_budget=definition.pg_connection_budget, pg_per_process=definition.pg_connections_per_process)

    def _fresh_api_sample(self, resource_id: str) -> tuple[float, int, float | None] | None:
        sample = self._api_load.get(resource_id)
        return sample if sample is not None and time.monotonic() - sample[0] <= 10 else None

    async def _retire_expired(self, resources: list[ChatRuntimeResource], fence: int,
                              definition: PoolConfig) -> str | None:
        """Keep the live guest until its durable replacement is ready and ingress applied."""
        reason = None
        healthy_workers = (await store.healthy_worker_resources(resources[0].pool_id)
                           if resources and definition.role == "worker" else set())
        for resource in list(resources):
            if (resource.observed_state != "ready" or resource.drain_requested_at is not None
                    or resource.desired_state == "deleting" or not self._retirement_due(resource, definition)):
                continue
            if definition.max_surge == 0:
                reason = "cloud_quota"
                continue
            replacements = [r for r in resources if r.drain_reason == "replacement:" + resource.id
                            and r.observed_state != "deleted"]
            if replacements:
                replacement = replacements[0]
                ready = replacement.observed_state == "ready" and replacement.desired_state != "deleting"
                if ready and replacement.role == "worker":
                    ready = replacement.id in healthy_workers
                if ready and replacement.role == "api":
                    ingress = self.ingresses.get(definition.name)
                    member = (await self._cloud(ingress.inspect, replacement.id, replacement.generation,
                              replacement.address)) if ingress is not None else None
                    ready = (self._fresh_api_sample(replacement.id) is not None
                             and member is not None and member.healthy and member.enabled)
                if ready:
                    await store.begin_drain(resource.id, resource.generation, self.owner, fence,
                                            reason="replacement")
                reason = "drain_pending"
                continue
            physical_limit = self._physical_limit(definition)
            if physical_limit <= len(resources):
                reason = "db_budget"
                continue
            try:
                factory = get_session_factory()
                async with factory() as session:
                    pool = await session.get(ChatRuntimePool, resource.pool_id)
                replacement = await store.request_resource(pool, role=definition.role,
                    request_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
                    image_ref=pool.image_ref, policy_digest=pool.profile_digest,
                    deadline=datetime.now(UTC) + timedelta(seconds=definition.boot_timeout_seconds),
                    owner=self.owner, fence=fence, replacement_for=resource.id, physical_limit=physical_limit)
                resources.append(replacement)
                reason = "drain_pending"
            except store.ResourceUnavailable as exc:
                if exc.code == "pool_lease_lost":
                    raise PoolLeaseLost() from exc
                reason = "cloud_quota"
        return reason

    async def _advance_drain(self, resource: ChatRuntimeResource, fence: int, definition: PoolConfig) -> None:
        if resource.role == "worker":
            await store.request_delete(resource.id, self.owner, fence)
            return
        if resource.role != "api" or self.transport is None or not resource.address:
            return
        ingress = self.ingresses.get(definition.name)
        if ingress is None:
            return
        factory = get_session_factory()
        async with factory() as session, session.begin():
            row, lease = await store._locked_resource(session, resource.id, resource.generation)
            if row is None or not store._owns(lease, self.owner, fence) or row.drain_requested_at is None:
                raise PoolLeaseLost()
            lease.reconcile_lease_expires_at = store._now() + timedelta(seconds=30)
            response = await self.transport.request(address=row.address, port=row.port or self.config.listen_port,
                role="api", resource_id=row.id, generation=row.generation, method="POST", path="/v1/drain",
                certificate_fingerprints=frozenset(accepted_fingerprints(row)),
                body={"resource_id": row.id, "generation": row.generation, "fence": fence},
                deadline=datetime.now(UTC) + timedelta(seconds=10))
            state = json.loads(response)
            if (state.get("draining") is not True or type(state.get("drain_fence")) is not int
                    or state["drain_fence"] != fence):
                raise InternalTransportError("guest drain fence not applied")
        await self._fenced_cloud(resource, fence, ingress.drain,
            resource.ingress_member_id, resource.id, resource.generation, resource.address)
        self._clear_backoff(f"readyz:{resource.id}")
        if not await self._probe_api_load(resource, fence, definition):
            return
        if await store.request_delete(resource.id, self.owner, fence) is not None:
            await self._fenced_cloud(resource, fence, ingress.delete,
                resource.ingress_member_id, resource.id, resource.generation, resource.address)


    async def _fail_expired_waiters(self, resources: list[ChatRuntimeResource]) -> None:
        now = datetime.now(UTC)
        for resource in resources:
            if not resource.run_id or not resource.deadline_at or resource.observed_state in {"ready", "deleted"}:
                continue
            deadline = resource.deadline_at if resource.deadline_at.tzinfo else resource.deadline_at.replace(tzinfo=UTC)
            if now >= deadline:
                await lifecycle.fail_waiting_run(resource.run_id, error_code="resource_deadline_exceeded",
                                                 safe_message="Sandbox provisioning deadline exceeded")

    async def _scale(self, pool: ChatRuntimePool, fence: int, definition: PoolConfig,
                     resources: list[ChatRuntimeResource], *, retirement_reason: str | None = None) -> None:
        now = datetime.now(UTC)
        healthy_workers = (await store.healthy_worker_resources(pool.id)
                           if definition.role == "worker" else set())
        serving = [r for r in resources if r.observed_state == "ready" and r.accepting
                   and r.drain_requested_at is None and r.desired_state != "deleting"
                   and (definition.role != "worker" or r.id in healthy_workers)]
        booting = [r for r in resources if r.observed_state in {"requested", "creating", "booting"}
                   and r.desired_state != "deleting" and r.deadline_at is not None
                   and store._utc(r.deadline_at) > now]
        normal_booting = [r for r in booting if not (r.drain_reason or "").startswith("replacement:")]
        current = len(serving) + len(normal_booting)
        reason = retirement_reason
        queued, oldest = 0, None
        if definition.role == "worker":
            estimate = await store.observe_service_time(pool.id, self.owner, fence,
                                                        cold_ms=definition.cold_service_time_ms)
            active, queued, oldest = await store.eligible_worker_demand(pool.id)
            desired = worker_desired(pool.min_replicas, pool.max_replicas, active, queued,
                estimate / 1000, pool.target_wait_seconds, pool.slots_per_worker, len(normal_booting))
            low = queued == 0 and active < pool.slots_per_worker * max(1, len(serving)) * 0.5
            if any(r.observed_state == "ready" and r.drain_requested_at is None
                   and r.id not in healthy_workers for r in resources):
                reason = "telemetry_stale"
        else:
            samples = [self._fresh_api_sample(resource.id) for resource in serving]
            if (any(sample is None for sample in samples)
                    or any(r.observed_state == "unavailable" and r.drain_requested_at is None for r in resources)):
                desired = max(pool.min_replicas, current)
                low = False
                reason = "telemetry_stale"
            else:
                active = sum(sample[1] for sample in samples)
                p95 = max((sample[2] for sample in samples if sample[2] is not None), default=0)
                target = definition.ingress.api_target_active_requests
                desired = api_desired(pool.min_replicas, pool.max_replicas, active, target, p95,
                    definition.ingress.api_target_ttft_ms, len(normal_booting))
                low = active < max(1, len(serving)) * target * 0.5
        idle = [r for r in serving if r.idle_since is not None
                and (now - store._utc(r.idle_since)).total_seconds() >= definition.idle_seconds]
        decision = gate_scale(target=desired, current=current, high_samples=pool.high_demand_samples,
            low_since=store._utc(pool.low_demand_since).timestamp() if pool.low_demand_since else None,
            now=now.timestamp(), low_utilization=low, safe_to_drain=bool(idle),
            pool_size=definition.db_connections_per_process, overflow=0,
            db_connection_budget=definition.db_connection_budget,
            pg_connection_budget=definition.pg_connection_budget,
            pg_connections_per_process=definition.pg_connections_per_process,
            replacement_reserve=definition.max_surge)
        reason = reason or decision.reason
        occupied = len(resources)
        for _ in range(max(0, min(decision.desired - current, pool.max_replicas - occupied))):
            try:
                await store.request_resource(pool, role=definition.role,
                    request_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
                    image_ref=pool.image_ref, policy_digest=pool.profile_digest,
                    deadline=now + timedelta(seconds=definition.boot_timeout_seconds),
                    owner=self.owner, fence=fence, physical_limit=self._physical_limit(definition))
            except store.ResourceUnavailable as exc:
                if exc.code == "pool_lease_lost":
                    raise PoolLeaseLost() from exc
                reason = "cloud_quota"
                break
        if decision.desired > current and occupied >= pool.max_replicas:
            reason = "cloud_quota"
        removals = max(0, current - decision.desired)
        for resource in sorted(idle, key=lambda r: store._utc(r.idle_since)):
            if not removals:
                break
            drained = await store.begin_drain(resource.id, resource.generation, self.owner, fence,
                reason="idle_window", idle_seconds=definition.idle_seconds)
            if drained is not None:
                try:
                    await self._advance_drain(drained, fence, definition)
                except PoolLeaseLost:
                    raise
                except Exception:
                    logger.exception("resource %s drain not applied", drained.id)
                    reason = "drain_pending"
                removals -= 1
        resources = await self._resources(pool.id)
        if (any(r.observed_state == "unknown" and r.desired_state != "deleting" for r in resources)
                or (definition.role == "api" and await store.unresolved_ingress_create(pool.id))):
            reason = "unknown_create"
        elif any(r.failure_code == "boot_timeout" for r in resources):
            reason = "boot_failed"
        elif any(r.drain_requested_at is not None or r.desired_state == "deleting" for r in resources):
            reason = reason if reason in {"db_budget", "cloud_quota", "telemetry_stale"} else "drain_pending"
        await store.write_projection(pool.id, self.owner, fence, desired=min(pool.max_replicas, decision.desired),
            ready=sum(r.observed_state == "ready" and r.accepting and r.drain_requested_at is None
                and r.desired_state != "deleting"
                and (r.id in healthy_workers if definition.role == "worker"
                     else self._fresh_api_sample(r.id) is not None) for r in resources),
            provisioning=sum(r.observed_state in {"requested", "creating", "booting"}
                and r.desired_state != "deleting" and r.deadline_at is not None
                and store._utc(r.deadline_at) > now for r in resources),
            draining=sum(r.drain_requested_at is not None or r.desired_state == "deleting" for r in resources),
            queued=queued, oldest=oldest, reason=reason,
            high_samples=decision.high_samples, low_since=decision.low_since)

    async def _reconcile_resource(self, resource: ChatRuntimeResource, pool: ChatRuntimePool,
                                  definition: PoolConfig, fence: int, provider,
                                  owned: list[Observation] | None) -> None:
        key = resource.id
        if not self._due(key):
            return
        try:
            if resource.observed_state == "unknown" and not resource.provider_id:
                await self._reconcile_unknown(resource, pool, definition, fence, owned)
            elif resource.desired_state == "deleting":
                await self._reconcile_delete(resource, definition, fence, provider)
            elif resource.observed_state == "requested":
                await self._reconcile_create(resource, definition, fence, provider)
            elif resource.observed_state in {"unknown", "creating", "booting", "ready", "unavailable", "draining"} and resource.provider_id:
                await self._reconcile_observe(resource, definition, fence, provider)
            self._clear_backoff(key)
        except PoolLeaseLost:
            raise
        except Exception:
            logger.exception("reconcile resource %s failed", resource.id)
            self._backoff(key)

    async def _reconcile_create(self, resource: ChatRuntimeResource, definition: PoolConfig,
                                fence: int, provider) -> None:
        operation = await store.claim_operation(resource.id, resource.generation, "create", self.owner, fence)
        if operation is None:
            return
        token: str | None = None
        if not provider.requires_delivery:
            try:
                token = await bootstrap.issue_bootstrap_token(resource.id, resource.generation, self.config)
            except Exception:
                await store.abort_unsubmitted_create(resource.id, resource.generation, self.owner, fence,
                                                     "bootstrap_unavailable")
                logger.exception("bootstrap token issuance failed for resource %s", resource.id)
                if resource.run_id:
                    await lifecycle.fail_waiting_run(resource.run_id, error_code="resource_unavailable",
                                                     safe_message="Sandbox provisioning unavailable")
                return
        try:
            observation = await self._fenced_cloud(resource, fence, provider.create,
                self._intent(resource, definition, bootstrap_token=token), action="create")
        except Exception:
            await store.mark_unknown(resource.id, resource.generation, self.owner, fence)
            raise
        await store.record_observation(resource.id, resource.generation, self.owner, fence, observation)

    async def _reconcile_unknown(self, resource: ChatRuntimeResource, pool: ChatRuntimePool,
                                 definition: PoolConfig, fence: int, owned: list[Observation] | None) -> None:
        matches = [item for item in owned or [] if item.metadata.get("lumen_deployment") == pool.deployment_id
                   and item.metadata.get("lumen_pool") == pool.name
                   and item.metadata.get("lumen_resource") == resource.id
                   and item.metadata.get("lumen_generation") == str(resource.generation)
                   and item.metadata.get("lumen_fingerprint") == resource.request_fingerprint
                   and item.metadata.get("lumen_policy") == resource.policy_digest]
        if len(matches) == 1:
            await store.record_observation(resource.id, resource.generation, self.owner, fence, matches[0])
        elif len(matches) > 1:
            logger.error("duplicate owned resources for %s; quarantining pending operator review", resource.id)

    async def _reconcile_observe(self, resource: ChatRuntimeResource, definition: PoolConfig,
                                 fence: int, provider) -> None:
        ref = self._ref(resource, definition)
        if ref is None:
            return
        observation = await self._cloud(provider.observe, ref)
        if observation.state.lower() == "absent":
            await store.mark_failed_with_cleanup(resource.id, resource.generation, self.owner, fence,
                                                 "resource_vanished")
            await store.claim_operation(resource.id, resource.generation, "delete", self.owner, fence)
            await self._prove_deleted(resource, definition, fence)
            if resource.run_id:
                await lifecycle.fail_waiting_run(resource.run_id, error_code="resource_unavailable",
                                                 safe_message="Assigned resource is no longer available")
            return
        if not await store.record_observation(resource.id, resource.generation, self.owner, fence, observation):
            return
        if (resource.ready_at is None and resource.drain_requested_at is None
                and resource.observed_state not in {"ready", "draining"}
                and resource.deadline_at is not None and store._utc(resource.deadline_at) <= datetime.now(UTC)):
            if resource.role == "api" and (resource.ingress_member_id is not None
                    or await store.unresolved_ingress_create(resource.pool_id, resource_id=resource.id)):
                # A lost member-create response may already have exposed this guest.
                # Adopt first; never reclaim potential HTTP/SSE/WS sessions as a failed boot.
                return
            # Full identity ownership was verified above; unknown creates remain occupied.
            await store.mark_failed_with_cleanup(resource.id, resource.generation, self.owner, fence, "boot_timeout")
            return
        if (provider.requires_delivery and observation.state.lower() in {"created", "stopped"}
                and not resource.bootstrap_token_hash and not resource.certificate_fingerprint):
            try:
                token = await bootstrap.issue_bootstrap_token(resource.id, resource.generation, self.config)
            except bootstrap.BootstrapRejected:
                return
            delivered = await self._fenced_cloud(resource, fence, provider.deliver_bootstrap, ref, token)
            if not delivered:
                logger.warning("bootstrap delivery not confirmed for resource %s", resource.id)
        elif (provider.requires_delivery and resource.bootstrap_expires_at and not resource.certificate_fingerprint
              and (resource.bootstrap_expires_at.replace(tzinfo=UTC) if resource.bootstrap_expires_at.tzinfo is None
                   else resource.bootstrap_expires_at) <= datetime.now(UTC)):
            await store.mark_failed_with_cleanup(resource.id, resource.generation, self.owner, fence,
                                                 "bootstrap_expired")

    async def _withdraw_ingress(self, resource: ChatRuntimeResource, definition: PoolConfig, fence: int) -> bool:
        ingress = self.ingresses.get(definition.name)
        if resource.role != "api" or ingress is None or not resource.address:
            return True
        if resource.ingress_member_id is None:
            member = await self._cloud(ingress.inspect, resource.id, resource.generation, resource.address)
            if member is None:
                # An empty member list cannot disprove a committed, ambiguous create.
                return not await store.unresolved_ingress_create(resource.pool_id, resource_id=resource.id)
            if not await store.record_ingress_member(resource.id, resource.generation, self.owner, fence, member.id):
                raise PoolLeaseLost()
            resource.ingress_member_id = member.id
        await self._fenced_cloud(resource, fence, ingress.drain,
            resource.ingress_member_id, resource.id, resource.generation, resource.address)
        await self._fenced_cloud(resource, fence, ingress.delete,
            resource.ingress_member_id, resource.id, resource.generation, resource.address)
        return True

    async def _prove_deleted(self, resource: ChatRuntimeResource, definition: PoolConfig,
                             fence: int) -> None:
        if not await self._withdraw_ingress(resource, definition, fence):
            return
        await store.prove_absent(resource.id, resource.generation, self.owner, fence)


    async def _reconcile_delete(self, resource: ChatRuntimeResource, definition: PoolConfig,
                                fence: int, provider) -> None:
        ref = self._ref(resource, definition)
        if not await self._withdraw_ingress(resource, definition, fence):
            return
        if ref is None:
            # A create may still be in flight or its response lost; no absence proof
            # exists until it either surfaces via ownership listing or the create
            # operation itself is durably unclaimed/never attempted. `prove_absent`
            # itself refuses while a claimed create could still land.
            if resource.observed_state in {"requested", "unknown"}:
                await store.prove_absent(resource.id, resource.generation, self.owner, fence)
            return
        operation = await store.claim_operation(resource.id, resource.generation, "delete", self.owner, fence)
        if operation is not None:
            try:
                result = await self._fenced_cloud(resource, fence, provider.delete, ref, action="delete")
            except Exception:
                await store.mark_unknown(resource.id, resource.generation, self.owner, fence, action="delete")
                raise
            if result.absent:
                await self._prove_deleted(resource, definition, fence)
            return
        # The one-shot delete may already be asynchronous or ambiguously accepted.
        # Explicit per-resource GET absence, never an empty list, releases occupancy.
        observation = await self._cloud(provider.observe, ref)
        if observation.state.lower() == "absent":
            await self._prove_deleted(resource, definition, fence)
