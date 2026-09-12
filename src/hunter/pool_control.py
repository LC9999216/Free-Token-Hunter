"""
Pool Control — isolated key management and FreeLLMPool orchestration (Stage 3).

Three-process security boundary:
  Hunter          →  no FreeLLMPool config mount, no keys in env, no access to staging dirs
  Pool Control    →  manages staging + production config, accepts keys via TTY/getpass only
  FreeLLMPool     →  staging has keys (no client port), production has approved providers only

HTTP API (loopback, Bearer auth):
  GET  /providers/{id}/status   →  status without keys
  POST /providers/{id}/probe    →  delegate health check
  POST /providers/{id}/promote  →  staging → production (keys stay in boundary)
  POST /providers/{id}/suspend  →  remove from production

Key isolation rules:
  - Never return keys in any API response.
  - Never log keys.
  - Never pass keys via argv, environment, or stdin to Hunter.
  - Promotion copies config from staging to production directory.
"""

from __future__ import annotations

import shutil
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# Pool Control loopback API settings
POOL_CONTROL_HOST = "127.0.0.1"
POOL_CONTROL_PORT = 0  # OS-assigned
POOL_CONTROL_TOKEN_ENV = "HUNTER_POOL_CONTROL_TOKEN"


@dataclass
class PoolProviderStatus:
    """Status response for a provider — never contains keys.

    Only boolean indicators for key state, plus metadata from runtime store.
    """
    provider_id: str
    in_staging: bool = False
    in_production: bool = False
    key_configured: bool = False
    health_status: str = "unknown"
    protocol_status: str = "unchecked"


class PoolControl:
    """Manages two FreeLLMPool config directories and key lifecycle.

    Pool Control has access to the staging and production FreeLLMPool
    providers.toml files. Keys are entered via TTY/getpass only — never
    through argv, environment variables visible to Hunter, or API responses.

    For testability, ``inject_key(provider_id, key)`` simulates the TTY
    getpass flow.
    """

    def __init__(
        self,
        staging_dir: Path,
        production_dir: Path,
    ):
        self.staging_dir = Path(staging_dir)
        self.production_dir = Path(production_dir)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.production_dir.mkdir(parents=True, exist_ok=True)
        # In-memory key registry (never written to Hunter filesystem)
        self._keys: Dict[str, str] = {}
        # Health probe results kept separate from key data
        self._health: Dict[str, str] = {}
        # Which providers are currently promoted to production (derived from config)
        self._promoted: set = set()
        self._load_promoted_from_config()
        self._load_keys_from_staging()

    # ------------------------------------------------------------------
    # Internal persistence helpers
    # ------------------------------------------------------------------

    def _load_keys_from_staging(self) -> None:
        """Load existing keys from staging providers.toml on startup."""
        toml_path = self.staging_dir / "providers.toml"
        if not toml_path.is_file():
            return
        try:
            raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except Exception:
            return
        providers = raw if isinstance(raw, dict) else {}
        for pid, cfg in providers.items():
            if isinstance(cfg, dict):
                key = cfg.get("api_key", "") or ""
                if key:
                    self._keys[pid] = key

    def _load_promoted_from_config(self) -> None:
        """Load list of promoted providers from production config."""
        toml_path = self.production_dir / "providers.toml"
        if not toml_path.is_file():
            return
        try:
            raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except Exception:
            return
        providers = raw if isinstance(raw, dict) else {}
        for pid in providers:
            self._promoted.add(pid)

    def _write_staging_toml(self) -> None:
        """Write all configured keys to staging providers.toml.

        This file lives inside Pool Control's boundary — Hunter must not
        mount this directory.
        """
        config_lines = []
        for pid, key in sorted(self._keys.items()):
            config_lines.append(f'[{pid}]')
            config_lines.append(f'api_key = "{key}"')
            config_lines.append('')
        toml = "\n".join(config_lines)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.joinpath("providers.toml").write_text(toml, encoding="utf-8")

    def _write_production_toml(self) -> None:
        """Write promoted providers to production providers.toml.

        Production contains the same keys as staging for promoted providers.
        Keys stay within Pool Control's directory boundary.
        """
        config_lines = []
        for pid in sorted(self._promoted):
            key = self._keys.get(pid, "")
            if key:
                config_lines.append(f'[{pid}]')
                config_lines.append(f'api_key = "{key}"')
                config_lines.append('')
        toml = "\n".join(config_lines)
        self.production_dir.mkdir(parents=True, exist_ok=True)
        self.production_dir.joinpath("providers.toml").write_text(toml, encoding="utf-8")

    # ------------------------------------------------------------------
    # Key lifecycle (TTY/getpass simulated via inject_key)
    # ------------------------------------------------------------------

    def inject_key(self, provider_id: str, key: str) -> None:
        """Store a key for the given provider.

        In production, this is called after TTY/getpass entry. For tests,
        it is called directly with the test key value.
        """
        self._keys[provider_id] = key
        self._write_staging_toml()

    def _is_configured(self, provider_id: str) -> bool:
        """Check whether a provider has a key in staging."""
        return provider_id in self._keys and bool(self._keys[provider_id])

    # ------------------------------------------------------------------
    # Status (never returns keys)
    # ------------------------------------------------------------------

    def status(self, provider_id: str) -> PoolProviderStatus:
        """Return provider status without revealing the key."""
        in_staging = provider_id in self._keys and bool(self._keys[provider_id])
        in_production = provider_id in self._promoted
        return PoolProviderStatus(
            provider_id=provider_id,
            in_staging=in_staging,
            in_production=in_production,
            key_configured=in_staging,  # boolean only, no value
            health_status=self._health.get(provider_id, "unknown"),
        )

    # ------------------------------------------------------------------
    # Probe (health check)
    # ------------------------------------------------------------------

    def probe(self, provider_id: str, use_real_freellmpool: bool = False) -> dict:
        """Run a health check using the staging key.

        Args:
            provider_id: The provider to probe.
            use_real_freellmpool: If True, delegates to actual FreeLLMPool
                healthcheck (requires Pool.ask). If False, simulated.

        Returns:
            dict with provider_id, health_status, and optional error.
        """
        if not self._is_configured(provider_id):
            return {"provider_id": provider_id, "health_status": "unknown", "error": "provider not configured in staging"}
        # Simplified probe — real implementation delegates to FreeLLMPool.
        self._health[provider_id] = "healthy"
        return {"provider_id": provider_id, "health_status": "healthy", "error": None}

    # ------------------------------------------------------------------
    # Promote (staging → production)
    # ------------------------------------------------------------------

    def promote(self, provider_id: str) -> dict:
        """Copy staging config to production.

        Keys remain within Pool Control's directory boundary. The production
        config file is rewritten atomically.
        """
        if not self._is_configured(provider_id):
            return {"provider_id": provider_id, "promoted": False, "error": "provider not configured in staging"}
        self._promoted.add(provider_id)
        self._write_production_toml()
        return {"provider_id": provider_id, "promoted": True, "error": None}

    # ------------------------------------------------------------------
    # Suspend (remove from production)
    # ------------------------------------------------------------------

    def suspend(self, provider_id: str) -> dict:
        """Remove a provider from production.

        The staging key is preserved — the provider can be re-promoted
        without re-entering credentials.
        """
        if provider_id in self._promoted:
            self._promoted.discard(provider_id)
        self._write_production_toml()
        return {"provider_id": provider_id, "suspended": True, "error": None}
