"""Stage 4 tests: Health, protocol, eligibility, approval orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from hunter.runtime.models import (
    ApprovalStatus,
    CredentialStatus,
    ExpectedPoolStatus,
    HealthStatus,
    ProtocolResult,
    RuntimeProvider,
    ActualPoolStatus,
)
from hunter.runtime.outbox import OutboxStore
from hunter.runtime.store import RuntimeStore
from hunter.runtime.orchestrator import (
    GateResult,
    LifecycleResult,
    check_credential_gate,
    check_health_gate,
    check_protocol_gate,
    check_approval_gate,
    check_eligibility,
    run_health_check,
    run_protocol_check,
    request_approval,
    promote_to_production,
    suspend_from_production,
    EligibilityError,
)
from hunter.pool_control import PoolControl


AS_OF = datetime.fromisoformat("2026-09-12T00:00:00+00:00")


def _make_store(tmp_path: Path) -> RuntimeStore:
    return RuntimeStore(tmp_path / "runtime_providers.json", tmp_path / "runtime_history.jsonl")


def _make_rp(
    credential_status=CredentialStatus.CONFIGURED,
    health_status=HealthStatus.HEALTHY,
    protocol=None,
    approval_status=ApprovalStatus.APPROVED,
    **kw,
) -> RuntimeProvider:
    if protocol is None:
        protocol = ProtocolResult(chat="pass", responses="pass", streaming="pass", tools="pass")
    return RuntimeProvider(
        provider_id=kw.get("provider_id", "acme"),
        provider_name=kw.get("provider_name", "Acme AI"),
        credential_status=credential_status,
        health_status=health_status,
        protocol_result=protocol,
        approval_status=approval_status,
    )


def _setup(tmp_path: Path) -> tuple[RuntimeStore, PoolControl]:
    store = _make_store(tmp_path)
    pc = PoolControl(tmp_path / "staging", tmp_path / "production")
    return store, pc


# =============================================================================
# Gate unit tests
# =============================================================================


def test_credential_gate_passes() -> None:
    rp = _make_rp(credential_status=CredentialStatus.CONFIGURED)
    g = check_credential_gate(rp)
    assert g.passed is True


def test_credential_gate_fails() -> None:
    rp = _make_rp(credential_status=CredentialStatus.NOT_CONFIGURED)
    g = check_credential_gate(rp)
    assert g.passed is False
    assert "NOT_CONFIGURED" in g.detail


def test_health_gate_passes() -> None:
    rp = _make_rp(health_status=HealthStatus.HEALTHY)
    g = check_health_gate(rp)
    assert g.passed is True


def test_health_gate_fails() -> None:
    rp = _make_rp(health_status=HealthStatus.DOWN)
    g = check_health_gate(rp)
    assert g.passed is False


def test_protocol_gate_passes() -> None:
    pr = ProtocolResult(chat="pass", responses="pass", streaming="pass", tools="pass")
    rp = _make_rp(protocol=pr)
    g = check_protocol_gate(rp)
    assert g.passed is True


def test_protocol_gate_fails_partial() -> None:
    pr = ProtocolResult(chat="pass", responses="fail", streaming="pass", tools="pass")
    rp = _make_rp(protocol=pr)
    g = check_protocol_gate(rp)
    assert g.passed is False
    assert "fail" in g.detail


def test_approval_gate_passes() -> None:
    rp = _make_rp(approval_status=ApprovalStatus.APPROVED)
    g = check_approval_gate(rp)
    assert g.passed is True


def test_approval_gate_pending() -> None:
    rp = _make_rp(approval_status=ApprovalStatus.PENDING)
    g = check_approval_gate(rp)
    assert g.passed is False


def test_approval_bypass() -> None:
    rp = _make_rp(approval_status=ApprovalStatus.PENDING)
    g = check_approval_gate(rp, require_approval=False)
    assert g.passed is True


# =============================================================================
# eligibility check
# =============================================================================


def test_eligibility_all_pass(tmp_path: Path) -> None:
    store, _ = _setup(tmp_path)
    rp = _make_rp()
    store.upsert(rp, reason="setup")
    result = check_eligibility("acme", store)
    assert result.success is True
    assert all(g.passed for g in result.gates)


def test_eligibility_credential_fails(tmp_path: Path) -> None:
    store, _ = _setup(tmp_path)
    rp = _make_rp(credential_status=CredentialStatus.NOT_CONFIGURED)
    store.upsert(rp, reason="setup")
    result = check_eligibility("acme", store)
    assert result.success is False
    assert result.gates[0].gate == "credential"
    assert result.gates[0].passed is False


def test_eligibility_health_fails(tmp_path: Path) -> None:
    store, _ = _setup(tmp_path)
    rp = _make_rp(health_status=HealthStatus.DOWN)
    store.upsert(rp, reason="setup")
    result = check_eligibility("acme", store)
    assert result.success is False


def test_eligibility_missing_provider(tmp_path: Path) -> None:
    store, _ = _setup(tmp_path)
    result = check_eligibility("nonexistent", store)
    assert result.success is False
    assert result.error is not None


# =============================================================================
# Health check lifecycle action
# =============================================================================


def test_health_check_updates_status(tmp_path: Path) -> None:
    store, pc = _setup(tmp_path)
    rp = _make_rp()
    store.upsert(rp, reason="setup")
    pc.inject_key("acme", "sk-test")
    result = run_health_check("acme", store, pc)
    assert result.success is True
    updated = store.get_provider("acme")
    assert updated.health_status is HealthStatus.HEALTHY
    assert updated.health_checked_at is not None


def test_health_check_missing_provider(tmp_path: Path) -> None:
    store, pc = _setup(tmp_path)
    result = run_health_check("nonexistent", store, pc)
    assert result.success is False


# =============================================================================
# Protocol check lifecycle action
# =============================================================================


def test_protocol_check_passes(tmp_path: Path) -> None:
    store, pc = _setup(tmp_path)
    rp = _make_rp()
    store.upsert(rp, reason="setup")
    result = run_protocol_check("acme", store, pc, simulate_all_pass=True)
    assert result.success is True
    updated = store.get_provider("acme")
    assert updated.protocol_result.all_pass() is True


def test_protocol_check_default_fails(tmp_path: Path) -> None:
    """Without simulate_all_pass, protocol check should not pass (no real API)."""
    store, pc = _setup(tmp_path)
    rp = _make_rp()
    store.upsert(rp, reason="setup")
    result = run_protocol_check("acme", store, pc, simulate_all_pass=False)
    assert result.success is True  # operation succeeds but protocol fails
    updated = store.get_provider("acme")
    assert updated.protocol_result.all_pass() is False


# =============================================================================
# Approval lifecycle action
# =============================================================================


def test_request_approval_sets_approved(tmp_path: Path) -> None:
    store, _ = _setup(tmp_path)
    rp = _make_rp(credential_status=CredentialStatus.CONFIGURED)
    store.upsert(rp, reason="setup")
    result = request_approval("acme", store, registry_free_score=85, evidence_digest="abc123", registry_revision=2)
    assert result.success is True
    updated = store.get_provider("acme")
    assert updated.approval_status is ApprovalStatus.APPROVED
    assert updated.expected_pool_status is ExpectedPoolStatus.STAGING
    assert updated.approval_binding is not None
    assert updated.approval_binding.free_score_snapshot == 85
    assert updated.approval_binding.evidence_digest == "abc123"


def test_request_approval_missing_provider(tmp_path: Path) -> None:
    store, _ = _setup(tmp_path)
    result = request_approval("nonexistent", store, 80, "digest", 1)
    assert result.success is False


# =============================================================================
# Production promotion lifecycle action
# =============================================================================


def test_promote_success(tmp_path: Path) -> None:
    store, pc = _setup(tmp_path)
    rp = _make_rp()
    store.upsert(rp, reason="setup")
    pc.inject_key("acme", "sk-test")
    pc.probe("acme")
    result = promote_to_production("acme", store, pc)
    assert result.success is True
    updated = store.get_provider("acme")
    assert updated.expected_pool_status is ExpectedPoolStatus.PRODUCTION
    assert updated.actual_pool_status is ActualPoolStatus.PRODUCTION


def test_promote_missing_credential_fails(tmp_path: Path) -> None:
    store, pc = _setup(tmp_path)
    rp = _make_rp(credential_status=CredentialStatus.NOT_CONFIGURED)
    store.upsert(rp, reason="setup")
    result = promote_to_production("acme", store, pc)
    assert result.success is False
    assert "promotion gates not met" in (result.error or "")


def test_promote_not_approved_fails(tmp_path: Path) -> None:
    store, pc = _setup(tmp_path)
    rp = _make_rp(approval_status=ApprovalStatus.PENDING)
    store.upsert(rp, reason="setup")
    result = promote_to_production("acme", store, pc)
    assert result.success is False


def test_promote_not_in_pool_control_fails(tmp_path: Path) -> None:
    """Provider with key configured in runtime but not in PoolControl staging."""
    store, pc = _setup(tmp_path)
    rp = _make_rp()
    store.upsert(rp, reason="setup")
    # Do NOT call pc.inject_key — PoolControl has no key for acme
    result = promote_to_production("acme", store, pc)
    assert result.success is False


def test_promote_enqueues_notification(tmp_path: Path) -> None:
    store, pc = _setup(tmp_path)
    outbox = OutboxStore(tmp_path / "outbox.json")
    rp = _make_rp()
    store.upsert(rp, reason="setup")
    pc.inject_key("acme", "sk-test")
    pc.probe("acme")
    result = promote_to_production("acme", store, pc, outbox_store=outbox)
    assert result.success is True
    assert len(outbox.pending()) == 1
    msg = outbox.pending()[0]
    assert msg.event_type == "PROMOTED_TO_PRODUCTION"


# =============================================================================
# Suspension lifecycle action
# =============================================================================


def test_suspend_removes_from_production(tmp_path: Path) -> None:
    store, pc = _setup(tmp_path)
    rp = _make_rp()
    store.upsert(rp, reason="setup")
    pc.inject_key("acme", "sk-test")
    pc.probe("acme")
    promote_to_production("acme", store, pc)
    assert store.get_provider("acme").actual_pool_status is ActualPoolStatus.PRODUCTION

    result = suspend_from_production("acme", store, pc, reason="health degradation")
    assert result.success is True
    updated = store.get_provider("acme")
    assert updated.actual_pool_status is ActualPoolStatus.SUSPENDED


def test_suspend_enqueues_notification(tmp_path: Path) -> None:
    store, pc = _setup(tmp_path)
    outbox = OutboxStore(tmp_path / "outbox.json")
    rp = _make_rp()
    store.upsert(rp, reason="setup")
    pc.inject_key("acme", "sk-test")
    result = suspend_from_production("acme", store, pc, reason="test", outbox_store=outbox)
    assert result.success is True
    assert len(outbox.pending()) == 1
    assert outbox.pending()[0].event_type == "POOL_SUSPENDED"


def test_suspend_preserves_staging_key(tmp_path: Path) -> None:
    """Even after suspension, PoolControl retains the staging key."""
    store, pc = _setup(tmp_path)
    rp = _make_rp()
    store.upsert(rp, reason="setup")
    pc.inject_key("acme", "sk-test")
    promote_to_production("acme", store, pc)
    suspend_from_production("acme", store, pc, reason="test")
    assert pc.status("acme").in_staging is True
    assert pc._is_configured("acme") is True
