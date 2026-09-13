"""Stage 2 runner — the full `hunter run-stage-two` lifecycle (plan §8).

Run order (authoritative, do not reorder):

1. Acquire the single-instance stage lock; recover pending runtime
   transactions. If another run is still active, skip and record it.
2. Read ALL RuntimeProviders — not just currently FREE_CONFIRMED ones.
3. Reconcile every provider against the Stage 1 Registry.
4. SUSPEND FIRST: any production provider whose registry status degraded
   (UNCERTAIN / EXPIRED / NOT_FREE / REJECTED), whose offer is no longer
   active, or whose approval binding is no longer valid, is suspended before
   anything new is imported (fail-closed suspend semantics).
5. Persist state, history, and outbox alerts.
6. Import newly FREE_CONFIRMED providers from the registry (runtime records
   only — pool registration and key entry stay manual operator actions).
7. Deliver outbox notifications (idempotent, retrying; offline without a
   configured adapter).
8. Run health and protocol checks for providers that have credentials
   configured.
9. Recompute eligibility (all gates, including the authoritative gate).
10. Promote ONLY providers holding a currently valid approval.
11. Re-verify the actual pool state against runtime expectations and emit a
    structured summary; any pool/runtime split halts production and alerts.

All of this is dependency-injected: tests drive the runner with a fake pool
control, fake probe runner, and fixed clock; production wires the real
Pool Control (or its loopback API client) and the real FreeLLMPool canaries.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..evidence.store import EvidenceStore
from ..pool_control import PoolControl
from ..registry.schema import ProviderStatus
from ..registry.store import ProviderRegistry
from .locks import LockHeldError, ProcessFileLock
from .models import (
    ActualPoolStatus,
    ApprovalStatus,
    CredentialStatus,
    ExpectedPoolStatus,
    HealthStatus,
    RuntimeProvider,
)
from .orchestrator import (
    check_authoritative_gate,
    promote_to_production,
    request_approval,
    run_health_check,
    run_protocol_check,
    suspend_from_production,
)
from .outbox import OutboxStore
from .store import RuntimeStore

logger = logging.getLogger("hunter.stage2")

# Registry statuses that disqualify a provider from staying in production.
_INVALID_PRODUCTION_STATUSES = {
    ProviderStatus.UNCERTAIN,
    ProviderStatus.EXPIRED,
    ProviderStatus.NOT_FREE,
    ProviderStatus.REJECTED,
}


class Stage2Error(Exception):
    """Structured Stage 2 runner failure."""


@dataclass
class Stage2Summary:
    """Structured, JSON-safe run summary (never contains key material)."""

    skipped_locked: bool = False
    recovered_transactions: int = 0
    providers_read: int = 0
    suspended: List[str] = field(default_factory=list)
    suspend_failed: List[str] = field(default_factory=list)
    imported: List[str] = field(default_factory=list)
    health_checked: List[str] = field(default_factory=list)
    protocol_checked: List[str] = field(default_factory=list)
    eligible: List[str] = field(default_factory=list)
    promoted: List[str] = field(default_factory=list)
    promotion_failed: List[str] = field(default_factory=list)
    pool_runtime_split: List[str] = field(default_factory=list)
    notifications_delivered: int = 0
    production_halted: bool = False
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skipped_locked": self.skipped_locked,
            "recovered_transactions": self.recovered_transactions,
            "providers_read": self.providers_read,
            "suspended": list(self.suspended),
            "suspend_failed": list(self.suspend_failed),
            "imported": list(self.imported),
            "health_checked": list(self.health_checked),
            "protocol_checked": list(self.protocol_checked),
            "eligible": list(self.eligible),
            "promoted": list(self.promoted),
            "promotion_failed": list(self.promotion_failed),
            "pool_runtime_split": list(self.pool_runtime_split),
            "notifications_delivered": self.notifications_delivered,
            "production_halted": self.production_halted,
            "notes": list(self.notes),
        }


class Stage2Runner:
    """One full Stage 2 run over the persisted state."""

    def __init__(
        self,
        *,
        data_dir: Path,
        pool_control: PoolControl,
        registry: Optional[ProviderRegistry] = None,
        evidence_store: Optional[EvidenceStore] = None,
        outbox_path: Optional[Path] = None,
        notification_adapter: Optional[Any] = None,
        clock: Optional[Callable[[], datetime]] = None,
        require_lock: bool = True,
        stage_lock: Optional[ProcessFileLock] = None,
    ):
        self.data_dir = Path(data_dir)
        self.pool_control = pool_control
        self.registry = registry or ProviderRegistry(
            self.data_dir / "providers.json", self.data_dir / "history.jsonl"
        )
        self.evidence_store = evidence_store or EvidenceStore(self.data_dir / "evidence.json")
        self.outbox_path = outbox_path or (self.data_dir / "notification_outbox.json")
        self.notification_adapter = notification_adapter
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._require_lock = require_lock
        self._stage_lock = stage_lock
        self._lock: Optional[ProcessFileLock] = None

    # -- helpers -----------------------------------------------------------

    def _runtime_store(self) -> RuntimeStore:
        return RuntimeStore(
            self.data_dir / "runtime_providers.json",
            self.data_dir / "runtime_history.jsonl",
        )

    def _open_outbox(self) -> OutboxStore:
        return OutboxStore(self.outbox_path)

    # -- the run -----------------------------------------------------------

    def run(self) -> Stage2Summary:
        summary = Stage2Summary()

        # (1) single-instance lock + transaction recovery
        if self._require_lock:
            self._lock = self._stage_lock or ProcessFileLock(self.data_dir / ".stage2.lock")
            try:
                self._lock.acquire(timeout=0.0, reentrant=False)
            except LockHeldError:
                summary.skipped_locked = True
                summary.notes.append("previous run still active; skipped")
                return summary
        try:
            store = self._runtime_store()
            store.recover_pending_transaction()

            # (2) read ALL runtime providers
            providers = store.list_providers()
            summary.providers_read = len(providers)

            # (3)+(4) reconcile registry, suspend invalid FIRST
            self._suspend_invalid_first(store, providers, summary)

            # (5) state/history/outbox persistence happened inside the
            # suspend calls (fail-closed semantics); nothing extra here.

            # (6) import new FREE_CONFIRMED providers
            self._import_new_confirmed(store, summary)

            # (7) deliver outbox notifications (idempotent consumer)
            summary.notifications_delivered = self._deliver_notifications()

            # (8) health + protocol checks for credentialed providers
            self._run_checks(store, summary)

            # (9) eligibility recompute
            for rp in store.list_providers():
                from .orchestrator import check_eligibility

                result = check_eligibility(
                    rp.provider_id, store, self.registry, self.evidence_store
                )
                if result.success:
                    summary.eligible.append(rp.provider_id)

            # (10) promote ONLY currently-approved providers
            self._promote_approved(store, summary)

            # (11) re-verify pool state; halt on split
            self._verify_pool_state(store, summary)
        finally:
            if self._lock is not None:
                self._lock.release()
        return summary

    # -- step 3/4: reconcile + suspend-first ---------------------------------

    def _suspend_invalid_first(
        self,
        store: RuntimeStore,
        providers: List[RuntimeProvider],
        summary: Stage2Summary,
    ) -> None:
        for rp in providers:
            if rp.actual_pool_status != ActualPoolStatus.PRODUCTION:
                continue
            registry_provider = self.registry.get_provider(rp.provider_id)
            reason: Optional[str] = None
            if registry_provider is None:
                reason = "registry_record_missing"
            elif registry_provider.status in _INVALID_PRODUCTION_STATUSES:
                reason = f"registry_status_{registry_provider.status.value.lower()}"
            else:
                offer = getattr(registry_provider, "free_offer", None)
                offer_status = getattr(offer, "offer_status", None) if offer else None
                if offer_status != "active":
                    reason = "offer_not_active"
                elif rp.approval_status != ApprovalStatus.APPROVED:
                    reason = "approval_invalidated"
                else:
                    gate = check_authoritative_gate(rp, self.registry, self.evidence_store)
                    if not gate.passed:
                        reason = f"approval_binding_{gate.detail}"
            if reason is None:
                continue
            result = suspend_from_production(
                rp.provider_id,
                store,
                self.pool_control,
                reason=reason,
                outbox_store=self._open_outbox(),
            )
            if result.success:
                summary.suspended.append(rp.provider_id)
            else:
                summary.suspend_failed.append(rp.provider_id)
                summary.notes.append(
                    f"suspend failed for {rp.provider_id}: {result.error}"
                )
                summary.production_halted = True

    # -- step 6: import new FREE_CONFIRMED ------------------------------------

    def _import_new_confirmed(self, store: RuntimeStore, summary: Stage2Summary) -> None:
        existing = {rp.provider_id for rp in store.list_providers()}
        for provider in self.registry.list_providers():
            if provider.id in existing:
                continue
            if provider.status != ProviderStatus.FREE_CONFIRMED:
                continue
            offer = getattr(provider, "free_offer", None)
            offer_status = getattr(offer, "offer_status", None) if offer else None
            if offer_status != "active":
                continue
            rp = RuntimeProvider(
                provider_id=provider.id,
                provider_name=provider.provider or provider.id,
                evidence_status=provider.status.value,
                credential_status=CredentialStatus.NOT_CONFIGURED,
                health_status=HealthStatus.UNKNOWN,
                expected_pool_status=ExpectedPoolStatus.DISABLED,
                actual_pool_status=ActualPoolStatus.UNKNOWN,
                approval_status=ApprovalStatus.PENDING,
            )
            store.upsert(rp, reason="stage2 import: FREE_CONFIRMED")
            summary.imported.append(provider.id)

    # -- step 7: notifications ---------------------------------------------------

    def _deliver_notifications(self) -> int:
        if self.notification_adapter is None:
            return 0  # offline: leave pending, no live credentials
        from .notify import OutboxConsumer

        outbox = self._open_outbox()
        try:
            consumer = OutboxConsumer(outbox, self.notification_adapter, clock=self._clock)
            result = consumer.process_once()
            return len(result.sent)
        finally:
            outbox.close()

    # -- step 8: health + protocol checks -----------------------------------------

    def _run_checks(self, store: RuntimeStore, summary: Stage2Summary) -> None:
        for rp in store.list_providers():
            if rp.credential_status != CredentialStatus.CONFIGURED:
                continue
            if rp.actual_pool_status == ActualPoolStatus.SUSPENDED:
                continue
            health = run_health_check(
                rp.provider_id, store, self.pool_control, clock=self._clock
            )
            if health.success:
                summary.health_checked.append(rp.provider_id)
            protocol = run_protocol_check(
                rp.provider_id, store, self.pool_control, clock=self._clock
            )
            if protocol.success:
                summary.protocol_checked.append(rp.provider_id)

    # -- step 10: promote approved only ----------------------------------------------

    def _promote_approved(self, store: RuntimeStore, summary: Stage2Summary) -> None:
        for rp in store.list_providers():
            if rp.approval_status != ApprovalStatus.APPROVED:
                continue
            if rp.expected_pool_status == ExpectedPoolStatus.PRODUCTION:
                continue  # already promoted
            if rp.actual_pool_status == ActualPoolStatus.SUSPENDED:
                continue  # must not re-promote a previously suspended provider
            result = promote_to_production(
                rp.provider_id,
                store,
                self.pool_control,
                registry=self.registry,
                evidence_store=self.evidence_store,
                outbox_store=self._open_outbox(),
                clock=self._clock,
            )
            if result.success:
                summary.promoted.append(rp.provider_id)
            else:
                summary.promotion_failed.append(rp.provider_id)

    # -- step 11: pool vs runtime verification -----------------------------------------

    def _verify_pool_state(self, store: RuntimeStore, summary: Stage2Summary) -> None:
        production_ids = set(self.pool_control.list_production())
        for rp in store.list_providers():
            in_pool = rp.provider_id in production_ids
            runtime_says_production = (
                rp.actual_pool_status == ActualPoolStatus.PRODUCTION
            )
            if in_pool and not runtime_says_production:
                # unknown-to-runtime provider serving traffic → containment
                self.pool_control.stop_production()
                summary.pool_runtime_split.append(rp.provider_id)
                summary.production_halted = True
            elif runtime_says_production and not in_pool:
                summary.pool_runtime_split.append(rp.provider_id)
                summary.notes.append(
                    f"{rp.provider_id} expected in production but missing from pool"
                )


def approve_provider(
    provider_id: str,
    expected_revision: int,
    *,
    data_dir: Path,
    approved_by: str = "user",
) -> Dict[str, Any]:
    """`hunter pool approve` worker: approve with a revision guard.

    The expected_revision guard makes the operator approve against a KNOWN
    registry state; the binding itself is derived from the authoritative
    stores (see orchestrator.request_approval).
    """
    registry = ProviderRegistry(data_dir / "providers.json", data_dir / "history.jsonl")
    evidence_store = EvidenceStore(data_dir / "evidence.json")
    store = RuntimeStore(
        data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl"
    )
    provider = registry.get_provider(provider_id)
    if provider is None:
        return {"ok": False, "error": "provider_not_in_registry"}
    if provider.revision != expected_revision:
        return {
            "ok": False,
            "error": (
                f"registry_revision_mismatch: expected {expected_revision}, "
                f"registry has {provider.revision} (re-review and retry)"
            ),
        }
    result = request_approval(
        provider_id, store, registry, evidence_store, approved_by=approved_by
    )
    return {
        "ok": result.success,
        "error": result.error,
        "provider_id": provider_id,
        "revision": provider.revision,
    }


def runtime_status(data_dir: Path) -> List[Dict[str, Any]]:
    """`hunter runtime status` worker: sanitized runtime provider listing."""
    store = RuntimeStore(
        data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl"
    )
    rows: List[Dict[str, Any]] = []
    for rp in store.list_providers():
        rows.append(
            {
                "provider_id": rp.provider_id,
                "provider_name": rp.provider_name,
                "evidence_status": rp.evidence_status,
                "credential_status": rp.credential_status.value,
                "health_status": rp.health_status.value,
                "protocol_all_pass": rp.protocol_result.all_pass(),
                "expected_pool_status": rp.expected_pool_status.value,
                "actual_pool_status": rp.actual_pool_status.value,
                "approval_status": rp.approval_status.value,
                "revision": rp.revision,
            }
        )
    return rows
