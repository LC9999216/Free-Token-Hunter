"""Auto-suspend — periodic health re-check with consecutive-failure threshold (Stage 5).

When a provider in production fails N consecutive health checks, it is
automatically suspended. The failure count resets on any successful check
or manual re-promotion.

Configurable via ``AutoSuspendPolicy``:
- max_consecutive_failures (default 3)
- check_interval_seconds (default 300)
- excluded_providers (set of provider_ids to never auto-suspend)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Set

from hunter.pool_control import PoolControl
from hunter.runtime.models import HealthStatus, RuntimeProvider
from hunter.runtime.store import RuntimeStore
from hunter.runtime.orchestrator import LifecycleResult, run_health_check, suspend_from_production


@dataclass
class AutoSuspendPolicy:
    """Policy governing automatic suspension of unhealthy production providers."""
    max_consecutive_failures: int = 3
    excluded_providers: Set[str] = field(default_factory=set)

    def is_excluded(self, provider_id: str) -> bool:
        return provider_id in self.excluded_providers


class AutoSuspendTracker:
    """Tracks consecutive health failures for each provider.

    Thread-safe for single-thread agent use; not designed for concurrent access.
    """

    def __init__(self, policy: AutoSuspendPolicy):
        self.policy = policy
        self._failures: Dict[str, int] = {}

    def record_outcome(self, provider_id: str, healthy: bool) -> None:
        """Record health check outcome.

        Args:
            provider_id: The checked provider.
            healthy: True if the health check passed.
        """
        if healthy:
            self._failures.pop(provider_id, None)
        else:
            self._failures[provider_id] = self._failures.get(provider_id, 0) + 1

    def should_suspend(self, provider_id: str) -> bool:
        """Check if the provider should be auto-suspended.

        Returns True if consecutive failures >= threshold and provider
        is not in the exclusion list.
        """
        if self.policy.is_excluded(provider_id):
            return False
        threshold = self.policy.max_consecutive_failures
        return self._failures.get(provider_id, 0) >= threshold

    def consecutive_failures(self, provider_id: str) -> int:
        return self._failures.get(provider_id, 0)

    def reset(self, provider_id: str) -> None:
        self._failures.pop(provider_id, None)


def run_auto_suspend_cycle(
    runtime_store: RuntimeStore,
    pool_control: PoolControl,
    tracker: AutoSuspendTracker,
) -> list[LifecycleResult]:
    """Run one auto-suspend cycle: health check all production providers,
    track failures, suspend any that hit the threshold.

    Returns a list of LifecycleResult (one per provider, with success=False
    for skipped providers and success=True for healthy/suspended ones).
    """
    results: list[LifecycleResult] = []
    for rp in runtime_store.list_providers():
        pid = rp.provider_id
        if rp.actual_pool_status != "PRODUCTION":
            continue

        # Run health check
        hc_result = run_health_check(pid, runtime_store, pool_control)
        healthy = hc_result.success and (runtime_store.get_provider(pid).health_status == HealthStatus.HEALTHY)

        tracker.record_outcome(pid, healthy)

        if tracker.should_suspend(pid):
            suspend_result = suspend_from_production(
                pid, runtime_store, pool_control,
                reason=f"auto-suspend (consecutive failures: {tracker.consecutive_failures(pid)})",
            )
            tracker.reset(pid)
            results.append(suspend_result)
        else:
            results.append(hc_result)

    return results
