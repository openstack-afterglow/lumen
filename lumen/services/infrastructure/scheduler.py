"""Pure capacity decisions; observations are supplied by the controller, not Redis."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ScaleDecision:
    desired: int
    high_samples: int
    low_since: float | None
    reason: str | None = None


def _clamp(minimum: int, maximum: int, value: int) -> int:
    if minimum < 0 or maximum < minimum:
        raise ValueError("invalid replica bounds")
    return min(maximum, max(minimum, value))


def worker_desired(
    min_replicas: int,
    max_replicas: int,
    active_slots: int,
    queued: int,
    service_time_s: float = 30,
    target_wait_s: float = 10,
    slots_per_worker: int = 4,
    provisioning: int = 0,
) -> int:
    """Total target, including provisioning; do not create target minus provisioning twice."""
    if min(active_slots, queued, provisioning) < 0 or min(service_time_s, target_wait_s, slots_per_worker) <= 0:
        raise ValueError("invalid worker demand")
    required = math.ceil((active_slots + queued * service_time_s / target_wait_s) / slots_per_worker)
    return _clamp(min_replicas, max_replicas, max(required, provisioning))


def api_desired(
    min_replicas: int,
    max_replicas: int,
    total_active: int,
    target_active: int,
    p95_ttft_ms: float,
    target_ttft_ms: float,
    provisioning: int = 0,
) -> int:
    if min(total_active, provisioning) < 0 or min(target_active, target_ttft_ms) <= 0 or p95_ttft_ms < 0:
        raise ValueError("invalid API demand")
    required = math.ceil(total_active / target_active)
    if p95_ttft_ms > target_ttft_ms and required and total_active > required * target_active * 0.7:
        required += 1
    return _clamp(min_replicas, max_replicas, max(required, provisioning))


def gate_scale(
    *,
    target: int,
    current: int,
    high_samples: int,
    low_since: float | None,
    now: float,
    low_utilization: bool,
    safe_to_drain: bool,
    pool_size: int,
    overflow: int,
    db_connection_budget: int | None,
) -> ScaleDecision:
    """Two consecutive high samples; 300s sustained low load and safe drain for scale-in."""
    if target > current:
        samples = high_samples + 1
        if samples < 2:
            return ScaleDecision(current, samples, None, "awaiting_second_demand_sample")
        if db_connection_budget is not None and target * (pool_size + overflow) > db_connection_budget:
            return ScaleDecision(current, samples, None, "db_connection_budget_exceeded")
        return ScaleDecision(target, samples, None)
    if target < current and low_utilization:
        since = now if low_since is None else low_since
        if now - since >= 300 and safe_to_drain:
            return ScaleDecision(target, 0, since)
        return ScaleDecision(current, 0, since, "scale_down_waiting_for_idle_drain")
    return ScaleDecision(current, 0, None)
