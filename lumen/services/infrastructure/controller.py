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
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from lumen.db import get_session_factory
from lumen.models.chat_infrastructure import ChatRuntimePool, ChatRuntimeResource, ChatWorkerRegistration
from lumen.services.durable_runs import lifecycle
from lumen.services.infrastructure import bootstrap, store
from lumen.services.infrastructure.config import PoolConfig, RuntimeConfig
from lumen.services.infrastructure.guest_api import api_load
from lumen.services.infrastructure.ingress import IngressProvider
from lumen.services.infrastructure.providers import Observation, ResourceIntent, ResourceRef
from lumen.services.infrastructure.scheduler import api_desired, gate_scale, worker_desired
from lumen.services.infrastructure.transport import InternalTransport, InternalTransportError

logger = logging.getLogger(__name__)

# Cloud states that only ever mean "the provider accepted the request"; never
# ready, and only "booting" once ownership/identity has already been verified.
_LIVE_STATES = {"active", "running", "ready", "booting", "build", "creating", "created"}
_DEAD_STATES = {"error", "failed"}


class ResourceController:
    def __init__(self, config: RuntimeConfig, providers: dict, *, owner: str,
                 db_pool_size: int = 20, db_overflow: int = 10):
        self.config = config
        self.providers = providers
        self.ingresses = {
            pool.name: IngressProvider(providers[pool.name].connection, pool.ingress)
            for pool in config.pools
            if pool.enabled and pool.role == "api" and pool.ingress is not None and pool.name in providers
        }
        self.owner = owner
        self.db_pool_size = db_pool_size
        self.db_overflow = db_overflow
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
        """Run one synchronous OpenStack SDK call off the event loop, bounded."""
        async with self.limiter:
            return await asyncio.get_running_loop().run_in_executor(self.executor, lambda: function(*args))

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
        for definition in self.config.pools:
            if not definition.enabled or definition.name not in self.providers:
                continue
            pool_id = self._pool_ids.get(definition.name)
            if pool_id is None:
                continue
            claim = await store.claim_pool_lease(pool_id, self.owner, self.config.pool_lease_seconds)
            if claim is None:
                continue
            pool, fence = claim
            await self.reconcile_pool(pool, fence, definition)

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
        if definition.role == "sandbox":
            await self._fail_expired_waiters(resources)
            return
        if definition.role == "worker":
            await store.list_stale_workers()
        await self._retire_expired(resources, fence, definition)
        await self._scale(pool, fence, definition, resources)

    async def _advance_readiness(self, resource: ChatRuntimeResource, pool: ChatRuntimePool,
                                  fence: int, definition: PoolConfig) -> None:
        if resource.observed_state == "ready":
            if resource.role == "sandbox":
                await store.wake_ready_runs(resource.id)
            elif (resource.role == "api" and resource.address and definition.name in self.ingresses
                  and await self._probe_api_load(resource, fence, definition)
                  and not self._retirement_due(resource, definition)):
                await self._ensure_ingress(resource, pool, fence, definition)
            return
        if (resource.observed_state not in {"booting", "unavailable"} or not resource.certificate_fingerprint
                or (resource.observed_state == "unavailable" and resource.role != "api")
                or (resource.role != "sandbox" and resource.role != "api"
                    and self._retirement_due(resource, definition))):
            return
        if resource.role == "api":
            if (resource.address and await self._probe_api_load(resource, fence, definition)
                    and definition.name in self.ingresses and not self._retirement_due(resource, definition)):
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
                certificate_fingerprint=resource.certificate_fingerprint, method="GET",
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
                certificate_fingerprint=resource.certificate_fingerprint, method="GET",
                path="/v1/ready", deadline=datetime.now(UTC) + timedelta(seconds=10),
            )
            metrics = api_load(response)
            if metrics is None:
                raise InternalTransportError("API readiness metrics unavailable")
            active, p95 = metrics
            if resource.observed_state != "ready":
                if not await store.mark_ready(resource.id, resource.generation, self.owner, fence, policy_verified=True):
                    return False
                resource.observed_state = "ready"
            if not await store.record_api_load(resource.id, resource.generation, self.owner, fence,
                                               active_count=active):
                return False
            resource.active_slots = active
            self._api_load[resource.id] = (time.monotonic(), active, p95)
            self._clear_backoff(key)
            return True
        except InternalTransportError:
            self._api_load.pop(resource.id, None)
            self._backoff(key)
            if resource.observed_state == "ready":
                if resource.ingress_member_id:
                    # Retain the resource if Octavia cannot confirm a disabled
                    # member; never re-enable an unverified process.
                    ingress = self.ingresses.get(definition.name)
                    if ingress is not None:
                        try:
                            await self._cloud(ingress.drain, resource.ingress_member_id)
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
            current = await self._cloud(ingress.inspect, resource.id, resource.address)
        except Exception:
            logger.exception("ingress lookup failed for resource %s", resource.id)
            return
        if current is not None and current.enabled and current.id == resource.ingress_member_id:
            return
        factory = get_session_factory()
        if factory is None:
            raise RuntimeError("database is not configured")
        try:
            # Limiter first: a coroutine holding the pool row lock never waits on the limiter.
            async with self.limiter, factory() as session, session.begin():
                row, lease = await store._locked_resource(session, resource.id, resource.generation)
                if (row is None or row.pool_id != pool.id or not store._owns(lease, self.owner, fence)
                        or row.role != "api" or row.observed_state != "ready" or row.desired_state == "deleting"
                        or row.address != resource.address
                        or self._retirement_due(row, definition)):
                    return
                member = await self._settled(ingress.register, row.id, row.address)
                if row.ingress_member_id != member.id:
                    row.ingress_member_id = member.id
        except Exception:
            logger.exception("ingress registration failed for resource %s", resource.id)
            return
        # Same-tick scale-in/retirement must drain the member just enabled.
        resource.ingress_member_id = member.id

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
        created = resource.created_at.replace(tzinfo=UTC) if resource.created_at.tzinfo is None else resource.created_at
        return ((now or datetime.now(UTC)) - created).total_seconds() >= definition.max_lifetime_seconds

    async def _retire_expired(self, resources: list[ChatRuntimeResource], fence: int,
                              definition: PoolConfig) -> None:
        """Drain expired resources; never force-delete a live lease or HTTP request."""
        now = datetime.now(UTC)
        for resource in resources:
            if resource.desired_state == "deleting" or not self._retirement_due(resource, definition, now=now):
                continue
            if resource.role == "worker":
                factory = get_session_factory()
                async with factory() as session:
                    rows = (await session.execute(select(ChatWorkerRegistration.id).where(
                        ChatWorkerRegistration.resource_id == resource.id))).scalars().all()
                for registration_id in rows:
                    await store.start_drain(registration_id)
            if resource.ingress_member_id and definition.name in self.ingresses:
                await self._cloud(self.ingresses[definition.name].drain, resource.ingress_member_id)
            created = resource.created_at.replace(tzinfo=UTC) if resource.created_at.tzinfo is None else resource.created_at
            if resource.active_slots:
                continue
            if resource.role == "api":
                sample = self._api_load.get(resource.id)
                if (not sample or sample[1] != 0
                        or time.monotonic() - sample[0] > max(15, self.config.reconcile_interval_seconds * 3)
                        or (now - created).total_seconds() < definition.max_lifetime_seconds + definition.drain_seconds):
                    continue
            await store.request_delete(resource.id, self.owner, fence)


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
                     resources: list[ChatRuntimeResource]) -> None:
        provisioning = sum(r.observed_state in {"requested", "creating", "unknown", "booting"} for r in resources)
        if definition.role == "worker":
            active, queued, _oldest = await store.eligible_worker_demand(pool.id)
            desired = worker_desired(pool.min_replicas, pool.max_replicas, active, queued,
                                     pool.service_time_estimate_ms / 1000, pool.target_wait_seconds,
                                     pool.slots_per_worker, provisioning)
            low = queued == 0 and active < pool.slots_per_worker * max(1, len(resources) or 1) * 0.5
        else:
            ready = [resource for resource in resources if resource.observed_state == "ready"]
            freshness = max(15, self.config.reconcile_interval_seconds * 3)
            samples = [self._api_load.get(resource.id) for resource in ready]
            if any(sample is None or time.monotonic() - sample[0] > freshness for sample in samples):
                # A missing sample never means zero traffic or authorizes scale-in.
                desired = max(pool.min_replicas, len(resources))
                low = False
            else:
                active = sum(sample[1] for sample in samples)
                p95 = max((sample[2] for sample in samples if sample[2] is not None), default=0)
                target = definition.ingress.api_target_active_requests
                desired = api_desired(pool.min_replicas, pool.max_replicas, active, target, p95,
                                      definition.ingress.api_target_ttft_ms, provisioning)
                low = active < max(1, len(ready)) * target * 0.5
        occupied = len(resources)
        now = datetime.now(UTC)
        decision = gate_scale(
            target=desired, current=occupied, high_samples=pool.high_demand_samples,
            low_since=pool.low_demand_since.timestamp() if pool.low_demand_since else None,
            now=now.timestamp(), low_utilization=low,
            safe_to_drain=all(r.active_slots == 0 for r in resources),
            pool_size=self.db_pool_size, overflow=self.db_overflow,
            db_connection_budget=pool.db_connection_budget,
        )
        factory = get_session_factory()
        async with factory() as session, session.begin():
            row = (await session.execute(select(ChatRuntimePool).where(
                ChatRuntimePool.id == pool.id).with_for_update())).scalar_one_or_none()
            if row is None or row.reconcile_lease_owner != self.owner or row.reconcile_fence != fence:
                return
            row.high_demand_samples = decision.high_samples
            row.low_demand_since = datetime.fromtimestamp(decision.low_since, UTC) if decision.low_since else None
        if decision.reason:
            logger.info("pool %s capacity hold: %s", pool.name, decision.reason)
        for _ in range(max(0, min(decision.desired - occupied, pool.max_replicas - occupied))):
            try:
                await store.request_resource(
                    pool, role=definition.role, request_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
                    image_ref=pool.image_ref, policy_digest=pool.profile_digest,
                    deadline=now + timedelta(seconds=definition.boot_timeout_seconds),
                    owner=self.owner, fence=fence,
                )
            except store.ResourceUnavailable:
                break
        pending_removal = sum(r.desired_state == "deleting" for r in resources)
        if decision.desired < occupied - pending_removal:
            for resource in resources:
                if occupied - pending_removal <= decision.desired:
                    break
                if resource.observed_state == "ready" and resource.active_slots == 0 and resource.desired_state != "deleting":
                    if resource.ingress_member_id and definition.name in self.ingresses:
                        try:
                            await self._cloud(self.ingresses[definition.name].drain, resource.ingress_member_id)
                            # Disabling a member races requests accepted just before drain.
                            # Probe once more after disable before releasing its capacity.
                            if not await self._probe_api_load(resource, fence, definition) or resource.active_slots:
                                continue
                        except Exception:
                            logger.exception("ingress drain failed for resource %s", resource.id)
                            continue
                    await store.request_delete(resource.id, self.owner, fence)
                    pending_removal += 1

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
            elif resource.observed_state in {"unknown", "creating", "booting", "ready", "unavailable"} and resource.provider_id:
                await self._reconcile_observe(resource, definition, fence, provider)
            self._clear_backoff(key)
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
            observation = await self._cloud(provider.create, self._intent(resource, definition, bootstrap_token=token))
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
        await store.record_observation(resource.id, resource.generation, self.owner, fence, observation)
        if (provider.requires_delivery and observation.state.lower() in {"created", "stopped"}
                and not resource.bootstrap_token_hash and not resource.certificate_fingerprint):
            try:
                token = await bootstrap.issue_bootstrap_token(resource.id, resource.generation, self.config)
            except bootstrap.BootstrapRejected:
                return
            delivered = await self._cloud(provider.deliver_bootstrap, ref, token)
            if not delivered:
                logger.warning("bootstrap delivery not confirmed for resource %s", resource.id)
        elif (provider.requires_delivery and resource.bootstrap_expires_at and not resource.certificate_fingerprint
              and (resource.bootstrap_expires_at.replace(tzinfo=UTC) if resource.bootstrap_expires_at.tzinfo is None
                   else resource.bootstrap_expires_at) <= datetime.now(UTC)):
            await store.mark_failed_with_cleanup(resource.id, resource.generation, self.owner, fence,
                                                 "bootstrap_expired")

    async def _prove_deleted(self, resource: ChatRuntimeResource, definition: PoolConfig,
                             fence: int) -> None:
        if resource.ingress_member_id and definition.name in self.ingresses:
            await self._cloud(self.ingresses[definition.name].delete, resource.ingress_member_id)
        await store.prove_absent(resource.id, resource.generation, self.owner, fence)


    async def _reconcile_delete(self, resource: ChatRuntimeResource, definition: PoolConfig,
                                fence: int, provider) -> None:
        ref = self._ref(resource, definition)
        if resource.ingress_member_id and definition.name in self.ingresses:
            await self._cloud(self.ingresses[definition.name].drain, resource.ingress_member_id)
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
                result = await self._cloud(provider.delete, ref)
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
