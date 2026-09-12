"""Stage 5 tests: auto-suspend, client config, orchestration, CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from hunter.auto_suspend import AutoSuspendPolicy, AutoSuspendTracker, run_auto_suspend_cycle
from hunter.client_config import generate_opencode_config, generate_codex_config, generate_agent_config
from hunter.pool_control import PoolControl
from hunter.runtime.models import (
    ApprovalStatus,
    CredentialStatus,
    ExpectedPoolStatus,
    HealthStatus,
    ProtocolResult,
    RuntimeProvider,
    ActualPoolStatus,
)
from hunter.runtime.store import RuntimeStore


# =============================================================================
# Auto-suspend
# =============================================================================


def test_tracker_records_failure() -> None:
    policy = AutoSuspendPolicy(max_consecutive_failures=3)
    tracker = AutoSuspendTracker(policy)
    tracker.record_outcome("acme", healthy=False)
    assert tracker.consecutive_failures("acme") == 1
    assert tracker.should_suspend("acme") is False


def test_tracker_resets_on_healthy() -> None:
    policy = AutoSuspendPolicy(max_consecutive_failures=3)
    tracker = AutoSuspendTracker(policy)
    tracker.record_outcome("acme", healthy=False)
    tracker.record_outcome("acme", healthy=False)
    tracker.record_outcome("acme", healthy=True)
    assert tracker.consecutive_failures("acme") == 0


def test_tracker_triggers_suspend() -> None:
    policy = AutoSuspendPolicy(max_consecutive_failures=3)
    tracker = AutoSuspendTracker(policy)
    for _ in range(3):
        tracker.record_outcome("acme", healthy=False)
    assert tracker.should_suspend("acme") is True


def test_tracker_excluded_providers() -> None:
    policy = AutoSuspendPolicy(max_consecutive_failures=2, excluded_providers={"acme"})
    tracker = AutoSuspendTracker(policy)
    for _ in range(5):
        tracker.record_outcome("acme", healthy=False)
    assert tracker.should_suspend("acme") is False


def test_tracker_reset_clears() -> None:
    policy = AutoSuspendPolicy(max_consecutive_failures=2)
    tracker = AutoSuspendTracker(policy)
    tracker.record_outcome("acme", healthy=False)
    tracker.reset("acme")
    assert tracker.consecutive_failures("acme") == 0


def test_auto_suspend_cycle_does_not_affect_non_production(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.json", tmp_path / "history.jsonl")
    pc = PoolControl(tmp_path / "staging", tmp_path / "production")
    rp = RuntimeProvider(
        provider_id="acme",
        credential_status=CredentialStatus.CONFIGURED,
        health_status=HealthStatus.HEALTHY,
        actual_pool_status=ActualPoolStatus.STAGING,  # not production
        approval_status=ApprovalStatus.APPROVED,
    )
    store.upsert(rp, reason="setup")
    policy = AutoSuspendPolicy(max_consecutive_failures=2)
    tracker = AutoSuspendTracker(policy)
    results = run_auto_suspend_cycle(store, pc, tracker)
    # No production providers → no health checks run
    assert all(r.success for r in results)


# =============================================================================
# Client config generation
# =============================================================================


def _production_rp(provider_id: str = "acme", **kw) -> RuntimeProvider:
    return RuntimeProvider(
        provider_id=provider_id,
        provider_name=kw.get("provider_name", f"{provider_id.capitalize()} AI"),
        credential_status=kw.get("credential_status", CredentialStatus.CONFIGURED),
        health_status=kw.get("health_status", HealthStatus.HEALTHY),
        protocol_result=kw.get("protocol_result", ProtocolResult(
            chat="pass", responses="pass", streaming="pass", tools="pass"
        )),
        actual_pool_status=ActualPoolStatus.PRODUCTION,
        expected_pool_status=ExpectedPoolStatus.PRODUCTION,
        approval_status=ApprovalStatus.APPROVED,
    )


def test_opencode_config_contains_production_providers() -> None:
    providers = [_production_rp("acme"), _production_rp("beta")]
    yaml = generate_opencode_config(providers)
    assert "acme" in yaml
    assert "beta" in yaml
    assert "providers:" in yaml
    assert "api_key" not in yaml


def test_opencode_config_skips_non_production() -> None:
    staging = RuntimeProvider(provider_id="staging-only", actual_pool_status=ActualPoolStatus.STAGING)
    providers = [_production_rp("prod"), staging]
    yaml = generate_opencode_config(providers)
    assert "prod" in yaml
    assert "staging-only" not in yaml


def test_opencode_config_includes_protocol_flags() -> None:
    pr = ProtocolResult(chat="pass", responses="unchecked", streaming="unchecked", tools="unchecked")
    rp = _production_rp(protocol_result=pr)
    yaml = generate_opencode_config([rp])
    assert "chat: true" in yaml
    assert "responses:" not in yaml  # Only pass values included
    assert "streaming:" not in yaml


def test_codex_config_has_base_url() -> None:
    yaml = generate_codex_config([_production_rp("acme")])
    assert "http://127.0.0.1:8080/v1/acme" in yaml
    assert "active: true" in yaml


def test_codex_config_capabilities() -> None:
    pr = ProtocolResult(chat="pass", responses="pass", streaming="pass", tools="unchecked")
    rp = _production_rp(protocol_result=pr)
    yaml = generate_codex_config([rp])
    assert "capabilities:" in yaml
    assert "chat" in yaml
    assert "streaming" in yaml


def test_agent_config_has_endpoint() -> None:
    yaml = generate_agent_config([_production_rp("acme")])
    assert "endpoint:" in yaml
    assert "http://127.0.0.1:8080/v1/acme" in yaml


def test_agent_config_actions() -> None:
    pr = ProtocolResult(chat="pass", responses="pass", streaming="unchecked", tools="pass")
    rp = _production_rp(protocol_result=pr)
    yaml = generate_agent_config([rp])
    assert "chat" in yaml
    assert "response" in yaml
    assert "tool_use" in yaml
    assert "streaming" not in yaml  # streaming is unchecked → not included


def test_config_no_secret_key_in_output() -> None:
    """Verification: no api_key, token, or secret key in generated YAML."""
    yaml = generate_opencode_config([_production_rp("acme")])
    assert "api_key" not in yaml
    assert "sk-" not in yaml
    assert "token" not in yaml.lower()

    yaml2 = generate_codex_config([_production_rp("acme")])
    assert "api_key" not in yaml2

    yaml3 = generate_agent_config([_production_rp("acme")])
    assert "api_key" not in yaml3


def test_empty_list_produces_valid_yaml() -> None:
    assert generate_opencode_config([]).strip() != ""
    assert generate_codex_config([]).strip() != ""
    assert generate_agent_config([]).strip() != ""
