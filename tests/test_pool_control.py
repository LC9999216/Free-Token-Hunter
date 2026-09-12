"""Stage 3 tests: Pool Control — secret isolation, loopback API, dual instances."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hunter.pool_control import PoolControl, PoolProviderStatus


# =============================================================================
# PoolControl — dual FreeLLMPool instance management
# =============================================================================


def test_pool_control_creates_directories(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    production = tmp_path / "production"
    pc = PoolControl(staging_dir=staging, production_dir=production)
    assert staging.exists()
    assert production.exists()
    assert staging.is_dir()
    assert production.is_dir()


def test_pool_control_empty_status(tmp_path: Path) -> None:
    pc = PoolControl(tmp_path / "staging", tmp_path / "production")
    status = pc.status("acme")
    assert status.provider_id == "acme"
    assert status.in_staging is False
    assert status.in_production is False
    assert status.key_configured is False
    assert status.health_status == "unknown"


def test_inject_key_creates_staging_config(tmp_path: Path) -> None:
    pc = PoolControl(tmp_path / "staging", tmp_path / "production")
    pc.inject_key("acme", "sk-test-key-abc123")
    status = pc.status("acme")
    assert status.in_staging is True
    assert status.key_configured is True
    # Verify the staging providers.toml was created
    toml_path = tmp_path / "staging" / "providers.toml"
    assert toml_path.is_file()
    content = toml_path.read_text(encoding="utf-8")
    assert "acme" in content
    assert "sk-test-key-abc123" in content  # key is in Pool Control's config dir


def test_key_not_in_status_response(tmp_path: Path) -> None:
    """PoolControl.status() must never return the key value."""
    pc = PoolControl(tmp_path / "staging", tmp_path / "production")
    pc.inject_key("acme", "sk-secret-key-999")
    status = pc.status("acme")
    # Ensure the key is not in any JSON-serialized form of the response
    status_json = json.dumps({
        "provider_id": status.provider_id,
        "in_staging": status.in_staging,
        "in_production": status.in_production,
        "key_configured": status.key_configured,
        "health_status": status.health_status,
    })
    assert "sk-secret-key-999" not in status_json
    assert "sk-" not in status_json  # key prefix also not present


def test_probe_requires_key(tmp_path: Path) -> None:
    pc = PoolControl(tmp_path / "staging", tmp_path / "production")
    result = pc.probe("acme")
    assert result["error"] is not None
    assert "not configured" in result["error"]


def test_probe_with_key_returns_healthy(tmp_path: Path) -> None:
    pc = PoolControl(tmp_path / "staging", tmp_path / "production")
    pc.inject_key("acme", "sk-test-key")
    result = pc.probe("acme")
    assert result["error"] is None
    assert result["health_status"] in ("healthy",)


def test_probe_updates_health_status(tmp_path: Path) -> None:
    pc = PoolControl(tmp_path / "staging", tmp_path / "production")
    pc.inject_key("acme", "sk-test-key")
    pc.probe("acme")
    status = pc.status("acme")
    assert status.health_status == "healthy"


def test_promote_without_staging_fails(tmp_path: Path) -> None:
    pc = PoolControl(tmp_path / "staging", tmp_path / "production")
    result = pc.promote("acme")
    assert result["promoted"] is False
    assert result["error"] is not None


def test_promote_copies_config(tmp_path: Path) -> None:
    pc = PoolControl(tmp_path / "staging", tmp_path / "production")
    pc.inject_key("acme", "sk-test-key-promote")
    result = pc.promote("acme")
    assert result["promoted"] is True
    assert result["error"] is None
    # Verify production config now has the key
    prod_toml = tmp_path / "production" / "providers.toml"
    assert prod_toml.is_file()
    content = prod_toml.read_text(encoding="utf-8")
    assert "acme" in content
    assert "sk-test-key-promote" in content
    # Status should reflect production
    status = pc.status("acme")
    assert status.in_production is True


def test_promote_does_not_expose_key_in_result(tmp_path: Path) -> None:
    """PoolControl.promote() result must not contain the key value."""
    pc = PoolControl(tmp_path / "staging", tmp_path / "production")
    pc.inject_key("acme", "sk-double-secret-key")
    result = pc.promote("acme")
    result_json = json.dumps(result)
    assert "sk-double-secret-key" not in result_json


def test_suspend_removes_from_production(tmp_path: Path) -> None:
    pc = PoolControl(tmp_path / "staging", tmp_path / "production")
    pc.inject_key("acme", "sk-test-key")
    pc.promote("acme")
    assert pc.status("acme").in_production is True

    result = pc.suspend("acme")
    assert result["suspended"] is True
    assert result["error"] is None
    status = pc.status("acme")
    assert status.in_production is False


def test_suspend_preserves_staging(tmp_path: Path) -> None:
    """Suspension only removes production config; staging key survives."""
    pc = PoolControl(tmp_path / "staging", tmp_path / "production")
    pc.inject_key("acme", "sk-test-key")
    pc.promote("acme")
    pc.suspend("acme")
    # Staging should still have the key
    assert pc.status("acme").in_staging is True
    assert pc.status("acme").key_configured is True


def test_multiple_providers_independent(tmp_path: Path) -> None:
    pc = PoolControl(tmp_path / "staging", tmp_path / "production")
    pc.inject_key("acme", "sk-acme-key")
    pc.inject_key("beta", "sk-beta-key")
    pc.promote("acme")

    acme_status = pc.status("acme")
    beta_status = pc.status("beta")
    assert acme_status.in_production is True
    assert beta_status.in_production is False
    assert beta_status.key_configured is True  # still in staging


def test_probe_uses_staging_key(tmp_path: Path) -> None:
    """Probe should use the staging key (not production) for correlation."""
    pc = PoolControl(tmp_path / "staging", tmp_path / "production")
    pc.inject_key("unknown", "sk-unknown-key")
    result = pc.probe("unknown")
    # Probe should find the key in staging
    assert result["error"] is None
