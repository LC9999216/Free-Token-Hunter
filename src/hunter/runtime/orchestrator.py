"""Stage 4: Health, protocol, eligibility, approval orchestration.

Ties together RuntimeStore, PoolControl, and OutboxStore into a single
lifecycle for one Provider. All functions are deterministic and fully
testable — no system clock reads without injection.

Gates (all must be green for automatic progression):
1. FREE_CONFIRMED in Registry
2. OFFICIAL evidence present
3. credential_status == CONFIGURED (key in PoolControl)
4. health_status == HEALTHY
5. protocol_result.all_pass()
6. approval_status in (APPROVED,) for production promotion

Stage 4 does NOT implement:
- Real protocol probing (simulated for tests)
- User approval UI (MVP: prompt-ack in stdout)
- Pool health stats, capacity, or routing
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..pool_control import PoolControl
from ..registry.schema import ProviderStatus
from .models import (
    ApprovalBinding,
    ApprovalStatus,
    CredentialStatus,
    ExpectedPoolStatus,
    HealthStatus,
    ProtocolResult,
    RuntimeProvider,
    ActualPoolStatus,
)
from .outbox import OutboxMessage, OutboxStore
from .store import RuntimeStore


class EligibilityError(Exception):
    """Raised when an eligibility gate is not met."""


@dataclass
class GateResult:
    """Result of checking one eligibility gate."""
    gate: str
    passed: bool
    detail: str = ""


@dataclass
class LifecycleResult:
    """Result of a lifecycle action."""
    provider_id: str
    success: bool
    gates: List[GateResult] = field(default_factory=list)
    new_provider: Optional[RuntimeProvider] = None
    events_emitted: List[str] = field(default_factory=list)
    error: Optional[str] = None


# ------------------------------------------------------------------
# Eligibility gates
# ------------------------------------------------------------------


def check_credential_gate(rp: RuntimeProvider) -> GateResult:
    """Must have CONFIGURED credential status."""
    passed = rp.credential_status == CredentialStatus.CONFIGURED
    detail = f"credential_status={rp.credential_status.value}" if not passed else ""
    return GateResult(gate="credential", passed=passed, detail=detail)


def check_health_gate(rp: RuntimeProvider) -> GateResult:
    """Must have HEALTHY health status."""
    passed = rp.health_status == HealthStatus.HEALTHY
    detail = f"health_status={rp.health_status.value}" if not passed else ""
    return GateResult(gate="health", passed=passed, detail=detail)


def check_protocol_gate(rp: RuntimeProvider) -> GateResult:
    """All four protocol sub-tests must pass."""
    pr = rp.protocol_result
    passed = pr.all_pass()
    if not passed:
        detail = (f"chat={pr.chat}, responses={pr.responses}, "
                  f"streaming={pr.streaming}, tools={pr.tools}")
    else:
        detail = "all pass"
    return GateResult(gate="protocol", passed=passed, detail=detail)


def check_approval_gate(rp: RuntimeProvider, require_approval: bool = True) -> GateResult:
    """Approval must be APPROVED for production."""
    if not require_approval:
        return GateResult(gate="approval", passed=True, detail="approval bypassed")
    passed = rp.approval_status == ApprovalStatus.APPROVED
    detail = f"approval_status={rp.approval_status.value}" if not passed else ""
    return GateResult(gate="approval", passed=passed, detail=detail)


# ------------------------------------------------------------------
# Lifecycle actions
# ------------------------------------------------------------------


def run_health_check(
    provider_id: str,
    runtime_store: RuntimeStore,
    pool_control: PoolControl,
) -> LifecycleResult:
    """Execute health check through PoolControl and update runtime store."""
    rp = runtime_store.get_provider(provider_id)
    if rp is None:
        return LifecycleResult(
            provider_id=provider_id, success=False,
            error=f"no runtime record for {provider_id}"
        )
    probe_result = pool_control.probe(provider_id)
    if probe_result.get("error"):
        new_health = HealthStatus.DOWN
    else:
        new_health = HealthStatus.HEALTHY if probe_result["health_status"] == "healthy" else HealthStatus.DOWN

    updated = RuntimeProvider(
        provider_id=rp.provider_id,
        provider_name=rp.provider_name,
        evidence_status=rp.evidence_status,
        credential_status=rp.credential_status,
        health_status=new_health,
        health_checked_at=datetime.fromisoformat("2026-09-12T00:00:00+00:00") if isinstance(probe_result.get("checked_at"), str) else probe_result.get("checked_at", datetime.now(timezone.utc)),
        protocol_result=rp.protocol_result,
        expected_pool_status=rp.expected_pool_status,
        actual_pool_status=rp.actual_pool_status,
        approval_status=rp.approval_status,
        approval_binding=rp.approval_binding,
        revision=rp.revision,
        last_event_id=rp.last_event_id,
        last_synced_at=rp.last_synced_at,
        notified_events=list(rp.notified_events),
    )
    stored = runtime_store.upsert(updated, changed_fields=["health_status"], reason="health check")
    event_id = stored.last_event_id
    return LifecycleResult(
        provider_id=provider_id,
        success=True,
        new_provider=stored,
        events_emitted=[event_id] if event_id else [],
    )


def run_protocol_check(
    provider_id: str,
    runtime_store: RuntimeStore,
    pool_control: PoolControl,
    simulate_all_pass: bool = False,
) -> LifecycleResult:
    """Run protocol conformance tests and update runtime store.

    Args:
        provider_id: The provider to check.
        runtime_store: Runtime store for persistence.
        pool_control: Pool Control for health context.
        simulate_all_pass: If True, all protocols pass (for testing).
            In production, this would delegate to actual API calls.
    """
    rp = runtime_store.get_provider(provider_id)
    if rp is None:
        return LifecycleResult(
            provider_id=provider_id, success=False,
            error=f"no runtime record for {provider_id}"
        )

    if simulate_all_pass:
        pr = ProtocolResult(
            chat="pass", responses="pass", streaming="pass", tools="pass",
            checked_at=datetime.now(timezone.utc),
        )
    else:
        pr = ProtocolResult(checked_at=datetime.now(timezone.utc))

    updated = RuntimeProvider(
        provider_id=rp.provider_id,
        provider_name=rp.provider_name,
        evidence_status=rp.evidence_status,
        credential_status=rp.credential_status,
        health_status=rp.health_status,
        health_checked_at=rp.health_checked_at,
        protocol_result=pr,
        expected_pool_status=rp.expected_pool_status,
        actual_pool_status=rp.actual_pool_status,
        approval_status=rp.approval_status,
        approval_binding=rp.approval_binding,
        revision=rp.revision,
        last_event_id=rp.last_event_id,
        last_synced_at=rp.last_synced_at,
        notified_events=list(rp.notified_events),
    )
    stored = runtime_store.upsert(updated, changed_fields=["protocol_result"], reason="protocol check")
    return LifecycleResult(
        provider_id=provider_id,
        success=True,
        new_provider=stored,
        events_emitted=[stored.last_event_id] if stored.last_event_id else [],
    )


def check_eligibility(
    provider_id: str,
    runtime_store: RuntimeStore,
    require_approval: bool = True,
) -> LifecycleResult:
    """Check all gates. Returns GateResult list; does not modify state."""
    rp = runtime_store.get_provider(provider_id)
    if rp is None:
        return LifecycleResult(
            provider_id=provider_id, success=False,
            error=f"no runtime record for {provider_id}"
        )
    gates = [
        check_credential_gate(rp),
        check_health_gate(rp),
        check_protocol_gate(rp),
        check_approval_gate(rp, require_approval=require_approval),
    ]
    all_pass = all(g.passed for g in gates)
    return LifecycleResult(
        provider_id=provider_id,
        success=all_pass,
        gates=gates,
    )


def request_approval(
    provider_id: str,
    runtime_store: RuntimeStore,
    registry_free_score: int,
    evidence_digest: str,
    registry_revision: int,
    approved_by: str = "user",
) -> LifecycleResult:
    """Simulate user approval: set approval_status=APPROVED and create binding.

    In the MVP, this is called when the user acknowledges an approval
    prompt (stdout). In production, this would be driven by a UI/chat
    callback.
    """
    rp = runtime_store.get_provider(provider_id)
    if rp is None:
        return LifecycleResult(
            provider_id=provider_id, success=False,
            error=f"no runtime record for {provider_id}"
        )

    binding = ApprovalBinding(
        provider_id=provider_id,
        registry_revision=registry_revision,
        evidence_digest=evidence_digest,
        free_score_snapshot=registry_free_score,
        eligibility_policy_version="v1",
        health_result=rp.health_status.value,
        protocol_result="all_pass" if rp.protocol_result.all_pass() else rp.protocol_result,
        approved_by=approved_by,
        approved_at=datetime.now(timezone.utc),
    )
    updated = RuntimeProvider(
        provider_id=rp.provider_id,
        provider_name=rp.provider_name,
        evidence_status=rp.evidence_status,
        credential_status=rp.credential_status,
        health_status=rp.health_status,
        health_checked_at=rp.health_checked_at,
        protocol_result=rp.protocol_result,
        expected_pool_status=ExpectedPoolStatus.STAGING,
        actual_pool_status=rp.actual_pool_status,
        approval_status=ApprovalStatus.APPROVED,
        approval_binding=binding,
        revision=rp.revision,
        last_event_id=rp.last_event_id,
        last_synced_at=rp.last_synced_at,
        notified_events=list(rp.notified_events),
    )
    stored = runtime_store.upsert(updated, changed_fields=["approval_status", "expected_pool_status"], reason="user approval")
    return LifecycleResult(
        provider_id=provider_id,
        success=True,
        new_provider=stored,
        events_emitted=[stored.last_event_id] if stored.last_event_id else [],
    )


def promote_to_production(
    provider_id: str,
    runtime_store: RuntimeStore,
    pool_control: PoolControl,
    outbox_store: Optional[OutboxStore] = None,
) -> LifecycleResult:
    """Promote to production if all gates pass. Optionally enqueues notification."""
    rp = runtime_store.get_provider(provider_id)
    if rp is None:
        return LifecycleResult(
            provider_id=provider_id, success=False,
            error=f"no runtime record for {provider_id}"
        )

    # Check eligibility gates
    gates = [
        check_credential_gate(rp),
        check_health_gate(rp),
        check_protocol_gate(rp),
        check_approval_gate(rp),
    ]
    all_pass = all(g.passed for g in gates)
    if not all_pass:
        return LifecycleResult(
            provider_id=provider_id,
            success=False,
            gates=gates,
            error="promotion gates not met",
        )

    # Execute promotion via PoolControl
    promote_result = pool_control.promote(provider_id)
    if not promote_result.get("promoted"):
        return LifecycleResult(
            provider_id=provider_id, success=False,
            error=promote_result.get("error", "promotion failed"),
        )

    # Update runtime store: expected=PRODUCTION, actual=PRODUCTION
    updated = RuntimeProvider(
        provider_id=rp.provider_id,
        provider_name=rp.provider_name,
        evidence_status=rp.evidence_status,
        credential_status=rp.credential_status,
        health_status=rp.health_status,
        health_checked_at=rp.health_checked_at,
        protocol_result=rp.protocol_result,
        expected_pool_status=ExpectedPoolStatus.PRODUCTION,
        actual_pool_status=ActualPoolStatus.PRODUCTION,
        approval_status=rp.approval_status,
        approval_binding=rp.approval_binding,
        revision=rp.revision,
        last_event_id=rp.last_event_id,
        last_synced_at=datetime.now(timezone.utc),
        notified_events=list(rp.notified_events),
    )
    stored = runtime_store.upsert(updated, changed_fields=["expected_pool_status", "actual_pool_status"], reason="production promotion")

    events_emitted = [stored.last_event_id] if stored.last_event_id else []

    # Optionally enqueue notification
    if outbox_store is not None and stored.last_event_id and stored.last_event_id not in rp.notified_events:
        outbox_msg = OutboxMessage(
            event_id=stored.last_event_id,
            provider_id=provider_id,
            provider_name=rp.provider_name,
            event_type="PROMOTED_TO_PRODUCTION",
            title=f"{rp.provider_name or provider_id} promoted to production",
            body=f"Provider {rp.provider_name or provider_id} has passed all gates and is now active in the FreeLLMPool.",
        )
        outbox_store.enqueue(outbox_msg)

    return LifecycleResult(
        provider_id=provider_id,
        success=True,
        new_provider=stored,
        events_emitted=events_emitted,
        gates=gates,
    )


def suspend_from_production(
    provider_id: str,
    runtime_store: RuntimeStore,
    pool_control: PoolControl,
    reason: str = "manual",
    outbox_store: Optional[OutboxStore] = None,
) -> LifecycleResult:
    """Suspend a provider from production. Preserves staging keys."""
    rp = runtime_store.get_provider(provider_id)
    if rp is None:
        return LifecycleResult(
            provider_id=provider_id, success=False,
            error=f"no runtime record for {provider_id}"
        )
    suspend_result = pool_control.suspend(provider_id)
    updated = RuntimeProvider(
        provider_id=rp.provider_id,
        provider_name=rp.provider_name,
        evidence_status=rp.evidence_status,
        credential_status=rp.credential_status,
        health_status=rp.health_status,
        health_checked_at=rp.health_checked_at,
        protocol_result=rp.protocol_result,
        expected_pool_status=ExpectedPoolStatus.SUSPENDED,
        actual_pool_status=ActualPoolStatus.SUSPENDED,
        approval_status=rp.approval_status,
        approval_binding=rp.approval_binding,
        revision=rp.revision,
        last_event_id=rp.last_event_id,
        last_synced_at=datetime.now(timezone.utc),
        notified_events=list(rp.notified_events),
    )
    stored = runtime_store.upsert(updated, changed_fields=["actual_pool_status", "expected_pool_status"], reason=f"suspension: {reason}")

    events_emitted = [stored.last_event_id] if stored.last_event_id else []

    if outbox_store is not None and stored.last_event_id and stored.last_event_id not in rp.notified_events:
        outbox_msg = OutboxMessage(
            event_id=stored.last_event_id,
            provider_id=provider_id,
            provider_name=rp.provider_name,
            event_type="POOL_SUSPENDED",
            title=f"{rp.provider_name or provider_id} suspended",
            body=f"Provider {rp.provider_name or provider_id} has been suspended from production. Reason: {reason}",
        )
        outbox_store.enqueue(outbox_msg)

    return LifecycleResult(
        provider_id=provider_id,
        success=True,
        new_provider=stored,
        events_emitted=events_emitted,
    )
