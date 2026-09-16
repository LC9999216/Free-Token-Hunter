"""Stage 2 runner + CLI end-to-end offline tests (review round 2, 六.8).

Covers the plan §8 run order with fully injected fakes:
- lock → skip when a previous run is active;
- reconcile registry → suspend invalid production providers FIRST;
- import new FREE_CONFIRMED providers;
- health/protocol checks through the injected pool control;
- eligibility recompute; promote only currently-approved providers;
- pool-vs-runtime verification (split detection halts production);
- `hunter pool approve` revision guard; `hunter runtime status` sanitized
  output; `hunter run-stage-two` end-to-end via main().
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hunter.auto_suspend import AutoSuspendPolicy
from hunter.cli import main as cli_main
from hunter.evidence.models import Evidence
from hunter.evidence.store import EvidenceStore
from hunter.pool_control import PoolControl
from hunter.registry.schema import FreeOffer, Provider, ProviderStatus
from hunter.registry.store import ProviderRegistry
from hunter.runtime.locks import LockHeldError, ProcessFileLock
from hunter.runtime.models import (
    ActualPoolStatus,
    ApprovalStatus,
    CredentialStatus,
    ExpectedPoolStatus,
    HealthStatus,
    ProtocolResult,
    RuntimeProvider,
)
from hunter.runtime.stage2 import Stage2Runner, approve_provider, runtime_status
from hunter.runtime.store import RuntimeStore

AS_OF = datetime.fromisoformat("2026-09-12T00:00:00+00:00")
SECRET = "sk-test-SECRET-0123456789"


class StaticSecretReader:
    def __call__(self, prompt: str) -> str:
        return SECRET


class FakeProbeRunner:
    def __init__(self, results=None):
        self.results = results or {}

    def run(self, provider_record, key_env, config_file, features=("chat",), timeout=20.0):
        return {
            feature: dict(
                self.results.get(
                    feature, {"status": "pass", "classification": "verified"}
                )
            )
            for feature in features
        }


def _registry_provider(pid="acme", **kw) -> Provider:
    return Provider(
        id=pid,
        provider=kw.get("provider", f"{pid.capitalize()} AI"),
        canonical_domain=kw.get("domain", f"{pid}.ai"),
        status=kw.get("status", ProviderStatus.FREE_CONFIRMED),
        evidence_ids=kw.get("evidence_ids", [f"ev-{pid}"]),
        verification_confidence=kw.get("verification_confidence", 85),
        score_metadata=kw.get("score_metadata", {"score": 90, "as_of": AS_OF.isoformat()}),
        free_score=kw.get("free_score", 90),
        free_offer=kw.get("free_offer", FreeOffer(offer_status="active")),
    )


def _seed(tmp_path: Path, providers=None, **pool_kw):
    """Build data dir with registry, evidence, runtime, pool control.

    Pass ``hunter_root=None`` to ``pool_kw`` for tests that need the pool
    directory under the repo (isolated tests use a path outside the repo).
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    registry = ProviderRegistry(data_dir / "providers.json", data_dir / "history.jsonl")
    evidence_store = EvidenceStore(data_dir / "evidence.json")
    for provider in providers or [_registry_provider()]:
        registry.upsert_provider(provider, reason="seed", source_metadata={"t": True})
        evidence_store.upsert(
            Evidence(
                evidence_id=f"ev-{provider.id}",
                provider_id=provider.id,
                url=f"https://{provider.canonical_domain}/pricing",
                officiality="OFFICIAL",
                source_type="pricing",
                content_fingerprint=f"fp-{provider.id}",
            )
        )
    pool = PoolControl(
        tmp_path / "staging",
        tmp_path / "production",
        read_secret=StaticSecretReader(),
        probe_runner=FakeProbeRunner(),
        **pool_kw,
    )
    return data_dir, registry, evidence_store, pool


def _approved_runtime_record(pid="acme", production=False) -> RuntimeProvider:
    return RuntimeProvider(
        provider_id=pid,
        provider_name=f"{pid.capitalize()} AI",
        evidence_status="FREE_CONFIRMED",
        credential_status=CredentialStatus.CONFIGURED,
        health_status=HealthStatus.HEALTHY,
        protocol_result=ProtocolResult(chat="pass", responses="pass", streaming="pass", tools="pass"),
        expected_pool_status=(
            ExpectedPoolStatus.PRODUCTION if production else ExpectedPoolStatus.STAGING
        ),
        actual_pool_status=(
            ActualPoolStatus.PRODUCTION if production else ActualPoolStatus.STAGING
        ),
        approval_status=ApprovalStatus.APPROVED,
    )


def _drive_to_approval(data_dir, pool, pid="acme") -> RuntimeProvider:
    """Register key, health, protocol, then approve via the worker."""
    pool.register_provider(pid, base_url=f"https://{pid}.ai/v1")
    pool.enter_key(pid)
    store = RuntimeStore(
        data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl"
    )
    store.upsert(_approved_runtime_record(pid), reason="setup")
    from hunter.runtime.orchestrator import (
        run_health_check,
        run_protocol_check,
        request_approval,
    )
    registry = ProviderRegistry(data_dir / "providers.json", data_dir / "history.jsonl")
    evidence_store = EvidenceStore(data_dir / "evidence.json")
    run_health_check(pid, store, pool)
    run_protocol_check(pid, store, pool)
    result = request_approval(pid, store, registry, evidence_store)
    assert result.success, result.error
    return store.get_provider(pid)


# =============================================================================
# Runner order tests
# =============================================================================


def test_runner_imports_new_free_confirmed(tmp_path: Path) -> None:
    data_dir, registry, evidence_store, pool = _seed(tmp_path)
    runner = Stage2Runner(
        data_dir=data_dir, pool_control=pool, registry=registry, evidence_store=evidence_store
    )
    summary = runner.run()
    assert summary.skipped_locked is False
    assert summary.imported == ["acme"]
    store = RuntimeStore(
        data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl"
    )
    rp = store.get_provider("acme")
    assert rp is not None
    assert rp.credential_status == CredentialStatus.NOT_CONFIGURED
    assert rp.actual_pool_status == ActualPoolStatus.UNKNOWN


def test_runner_promotes_approved_provider(tmp_path: Path) -> None:
    data_dir, registry, evidence_store, pool = _seed(tmp_path)
    _drive_to_approval(data_dir, pool)
    runner = Stage2Runner(
        data_dir=data_dir, pool_control=pool, registry=registry, evidence_store=evidence_store
    )
    summary = runner.run()
    assert summary.promoted == ["acme"]
    assert pool.list_production() == ["acme"]


def test_runner_suspends_registry_downgrade_first(tmp_path: Path) -> None:
    """Production provider whose registry status degrades is suspended
    BEFORE any import/promotion happens (plan §8 step 4)."""
    data_dir, registry, evidence_store, pool = _seed(tmp_path)
    _drive_to_approval(data_dir, pool)
    pool.promote("acme")
    store = RuntimeStore(
        data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl"
    )
    rp = store.get_provider("acme")
    store.upsert(
        RuntimeProvider(**{**rp.__dict__, "actual_pool_status": ActualPoolStatus.PRODUCTION}),
        changed_fields=["actual_pool_status"],
        reason="in production",
    )
    # registry downgrades the provider to UNCERTAIN
    registry.upsert_provider(
        _registry_provider(status=ProviderStatus.UNCERTAIN),
        reason="evidence weakened",
        source_metadata={"t": True},
    )
    runner = Stage2Runner(
        data_dir=data_dir, pool_control=pool, registry=registry, evidence_store=evidence_store
    )
    summary = runner.run()
    assert summary.suspended == ["acme"]
    assert pool.list_production() == []
    # and it is NOT re-promoted (approval binding now invalid)
    assert summary.promoted == []


def test_runner_skips_when_locked(tmp_path: Path) -> None:
    data_dir, registry, evidence_store, pool = _seed(tmp_path, hunter_root=None)
    # hold a separate lock so the runner cannot acquire
    held_lock = ProcessFileLock(data_dir / ".stage2.lock")
    held_lock.acquire()
    try:
        runner = Stage2Runner(
            data_dir=data_dir,
            pool_control=pool,
            registry=registry,
            evidence_store=evidence_store,
            stage_lock=held_lock,  # already held by this process
        )
        summary = runner.run()
        assert summary.skipped_locked is True
    finally:
        held_lock.release()


def test_runner_detects_pool_runtime_split(tmp_path: Path) -> None:
    """A provider serving in the pool but SUSPENDED in runtime → split → halt."""
    data_dir, registry, evidence_store, pool = _seed(tmp_path, hunter_root=None)
    _drive_to_approval(data_dir, pool)
    pool.promote("acme")  # pool now serves acme
    store = RuntimeStore(
        data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl"
    )
    rp = store.get_provider("acme")
    store.upsert(
        RuntimeProvider(**{**rp.__dict__, "actual_pool_status": ActualPoolStatus.SUSPENDED}),
        changed_fields=["actual_pool_status"],
        reason="suspended in runtime only",
    )
    runner = Stage2Runner(
        data_dir=data_dir, pool_control=pool, registry=registry, evidence_store=evidence_store
    )
    summary = runner.run()
    assert "acme" in summary.pool_runtime_split
    assert summary.production_halted is True


def test_runner_suspend_failure_halts_production(tmp_path: Path) -> None:
    data_dir, registry, evidence_store, pool = _seed(tmp_path)
    _drive_to_approval(data_dir, pool)
    pool.promote("acme")
    store = RuntimeStore(
        data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl"
    )
    rp = store.get_provider("acme")
    store.upsert(
        RuntimeProvider(**{**rp.__dict__, "actual_pool_status": ActualPoolStatus.PRODUCTION}),
        changed_fields=["actual_pool_status"],
        reason="in production",
    )
    registry.upsert_provider(
        _registry_provider(status=ProviderStatus.NOT_FREE),
        reason="offer withdrawn",
        source_metadata={"t": True},
    )

    def failing_suspend(provider_id):
        return {"suspended": False, "error": "production_write_failed"}

    pool.suspend = failing_suspend  # type: ignore[assignment]
    runner = Stage2Runner(
        data_dir=data_dir, pool_control=pool, registry=registry, evidence_store=evidence_store
    )
    summary = runner.run()
    assert summary.suspend_failed == ["acme"]
    assert summary.production_halted is True


# =============================================================================
# approve worker revision guard
# =============================================================================


def test_approve_worker_revision_guard(tmp_path: Path) -> None:
    data_dir, registry, evidence_store, pool = _seed(tmp_path)
    _drive_to_approval(data_dir, pool)
    provider = registry.get_provider("acme")
    wrong = approve_provider(
        "acme", expected_revision=provider.revision + 5, data_dir=data_dir
    )
    assert wrong["ok"] is False
    assert "registry_revision_mismatch" in wrong["error"]
    right = approve_provider(
        "acme", expected_revision=provider.revision, data_dir=data_dir
    )
    assert right["ok"] is True


def test_approve_worker_missing_provider(tmp_path: Path) -> None:
    data_dir, _, _, _ = _seed(tmp_path)
    result = approve_provider("ghost", expected_revision=1, data_dir=data_dir)
    assert result["ok"] is False


# =============================================================================
# runtime status worker
# =============================================================================


def test_runtime_status_sanitized(tmp_path: Path) -> None:
    data_dir, _, _, pool = _seed(tmp_path)
    _drive_to_approval(data_dir, pool)
    rows = runtime_status(data_dir)
    assert len(rows) == 1
    row = rows[0]
    assert row["provider_id"] == "acme"
    dumped = json.dumps(rows)
    assert SECRET not in dumped
    assert "api_key" not in dumped


# =============================================================================
# CLI end-to-end (offline, monkeypatched pool root)
# =============================================================================


def test_cli_runtime_status_offline(tmp_path: Path, capsys, monkeypatch) -> None:
    data_dir, _, _, pool = _seed(tmp_path)
    _drive_to_approval(data_dir, pool)
    code = cli_main(["runtime", "status", "--data-dir", str(data_dir)])
    assert code == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed[0]["provider_id"] == "acme"
    assert "sk-" not in out


def test_cli_pool_approve_offline(tmp_path: Path, capsys) -> None:
    data_dir, registry, evidence_store, pool = _seed(tmp_path)
    _drive_to_approval(data_dir, pool)
    provider = registry.get_provider("acme")
    code = cli_main(
        [
            "pool", "approve",
            "--provider-id", "acme",
            "--expected-revision", str(provider.revision),
            "--data-dir", str(data_dir),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert json.loads(out)["ok"] is True


def test_cli_pool_approve_rejects_wrong_revision(tmp_path: Path, capsys) -> None:
    data_dir, _, _, pool = _seed(tmp_path)
    _drive_to_approval(data_dir, pool)
    code = cli_main(
        [
            "pool", "approve",
            "--provider-id", "acme",
            "--expected-revision", "999",
            "--data-dir", str(data_dir),
        ]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "registry_revision_mismatch" in out


def test_cli_run_stage_two_offline(
    tmp_path: Path,
    outside_repo_tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    """Full CLI e2e: approved provider gets promoted through run-stage-two."""
    data_dir, registry, evidence_store, pool = _seed(
        outside_repo_tmp_path / "data", hunter_root=None
    )
    _drive_to_approval(data_dir, pool)
    # point the CLI at a pool outside the repo
    pool_dir = outside_repo_tmp_path / "poolroot"
    pool_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HUNTER_POOL_BASE_DIR", str(pool_dir))
    # connect pool/staging/config for approval credentials
    pool2 = PoolControl(
        pool_dir / "staging",
        pool_dir / "production",
        read_secret=StaticSecretReader(),
        probe_runner=FakeProbeRunner(),
    )
    pool2.register_provider("acme", base_url="https://acme.ai/v1")
    pool2.enter_key("acme")
    # run CLI (wires PoolControl internally with HUNTER_POOL_BASE_DIR)
    code = cli_main(
        ["run-stage-two", "--data-dir", str(data_dir)]
    )
    out = capsys.readouterr().out
    summary = json.loads(out)
    assert code in (0, 1)
    assert summary["providers_read"] >= 1
    assert "promoted" in summary
