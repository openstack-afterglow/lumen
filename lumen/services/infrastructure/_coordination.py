"""Redis scheduling hints; MariaDB leases remain the sole cloud write authority."""
from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass

from lumen.cache import _get_redis

PARTICIPATION_SECONDS = 30
HEARTBEAT_SECONDS = 5

# All keys share a deployment hash tag for Redis Cluster. Expiry and eligibility
# changes are observed atomically using Redis time, never controller wall clocks.
_SNAPSHOT = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now)
for _, owner in ipairs(expired) do
    redis.call('ZREM', KEYS[1], owner)
    redis.call('HDEL', KEYS[2], owner)
end
if ARGV[1] == 'heartbeat' then
    redis.call('ZADD', KEYS[1], now + tonumber(ARGV[3]), ARGV[2])
    redis.call('HSET', KEYS[2], ARGV[2], ARGV[4])
elseif ARGV[1] == 'leave' then
    redis.call('ZREM', KEYS[1], ARGV[2])
    redis.call('HDEL', KEYS[2], ARGV[2])
end
local owners = redis.call('ZRANGE', KEYS[1], 0, -1)
table.sort(owners)
local leader = ''
local entries = {}
for _, owner in ipairs(owners) do
    local eligible = redis.call('HGET', KEYS[2], owner)
    if eligible then
        if leader == '' then leader = owner end
        table.insert(entries, owner)
        table.insert(entries, eligible)
    end
end
if ARGV[1] == 'publish' and leader == ARGV[2] then
    redis.call('SET', KEYS[3], ARGV[4])
end
if #owners > 0 then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]) * 2)
    redis.call('EXPIRE', KEYS[2], tonumber(ARGV[3]) * 2)
end
return {leader, entries, redis.call('GET', KEYS[3]) or '{}'}
"""


def assign_pools(registry: dict, members: dict[str, dict[str, str]]) -> dict[str, str]:
    """Deterministic least-load assignment, rendezvous tie break, exact eligibility.

    Common eligible pool sets differ in count by at most one. Scarce eligible
    pools go first; incompatible definitions never receive a shared pool. No
    random iteration or process-local timing can change an unchanged assignment.
    """
    candidates = {
        name: sorted(owner for owner, eligible in members.items() if eligible.get(name) == target['digest'])
        for name, target in registry.items()
    }
    loads = dict.fromkeys(members, 0)
    assigned = {}
    owned = {owner: set() for owner in members}
    for name in sorted(registry, key=lambda name: (len(candidates[name]), name)):
        if not candidates[name]:
            continue
        # Follow alternating owner/pool paths so a flexible incumbent cannot
        # strand a late join that is eligible only for one of its pools.
        paths = {owner: (None, name, 0) for owner in candidates[name]}
        queue = deque(candidates[name])
        while queue:
            previous = queue.popleft()
            for pool in sorted(owned[previous]):
                for candidate in candidates[pool]:
                    if candidate not in paths:
                        paths[candidate] = (previous, pool, paths[previous][2] + 1)
                        queue.append(candidate)
        owner = min(paths, key=lambda owner: (
            loads[owner], paths[owner][2], hashlib.sha256(f'{name}\0{owner}'.encode()).digest(), owner,
        ))
        loads[owner] += 1
        while owner is not None:
            previous, pool, _ = paths[owner]
            if previous is not None:
                owned[previous].remove(pool)
            assigned[pool] = owner
            owned[owner].add(pool)
            owner = previous
    return assigned


@dataclass(frozen=True)
class Snapshot:
    leader: str | None
    registry: dict
    assignments: dict[str, str]


class PoolCoordinator:
    def __init__(self, deployment_id: str, owner: str, eligible: dict[str, str]):
        self.owner = owner
        self.eligible = eligible
        tag = hashlib.sha256(deployment_id.encode()).hexdigest()
        prefix = f'lumen:infrastructure:{{{tag}}}'
        self.keys = (f'{prefix}:participants', f'{prefix}:eligibility', f'{prefix}:registry')

    async def _snapshot(self, action: str, value: dict | None = None) -> Snapshot:
        redis = await _get_redis()
        leader, entries, registry = await redis.eval(
            _SNAPSHOT, len(self.keys), *self.keys, action, self.owner,
            PARTICIPATION_SECONDS, json.dumps(value or {}, sort_keys=True),
        )
        members = {
            (owner.decode() if isinstance(owner, bytes) else owner): json.loads(eligible)
            for owner, eligible in zip(entries[::2], entries[1::2], strict=True)
        }
        registry = json.loads(registry)
        leader = leader.decode() if isinstance(leader, bytes) else leader
        return Snapshot(leader or None, registry, assign_pools(registry, members))

    async def heartbeat(self) -> Snapshot:
        return await self._snapshot('heartbeat', self.eligible)

    async def current(self) -> Snapshot:
        # A dispatch check must not resurrect an expired controller incarnation.
        return await self._snapshot('current')

    async def publish_registry(self, registry: dict) -> bool:
        return (await self._snapshot('publish', registry)).leader == self.owner

    async def leave(self) -> None:
        await self._snapshot('leave')
