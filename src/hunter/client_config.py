"""Client config generators — Codex, OpenCode, generic Agent (Stage 5).

Generates configuration for downstream clients that consume the FreeLLMPool
production proxy. Security rules (review round 2, 七):

- Only READBACK-CONFIRMED production providers are included: callers pass the
  provider ids actually present in the production pool catalog (Pool Control
  readback), and a provider additionally needs actual_pool_status=PRODUCTION.
- Client admission profiles: Codex requires Responses + Streaming + Tools;
  OpenCode/OpenAI-compatible profiles require chat; generic agents include
  whatever capabilities are declared as passing.
- Authentication references environment variable NAMES only — no key, token,
  or Authorization material is ever written into a template.
- Serialization is done with yaml.safe_dump (PyYAML) / json.dumps / a
  quoting-safe TOML renderer — never string concatenation of untrusted ids.
- Every generated document is re-parsed and re-validated before it is
  returned; a document that does not round-trip fails closed.
- A final secret scan rejects any output containing credential-like content.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import yaml

from hunter.runtime.models import RuntimeProvider
from hunter.runtime.models import ActualPoolStatus

# Loopback proxy endpoint of the production FreeLLMPool.
PRODUCTION_PROXY_BASE = "http://127.0.0.1:8080/v1"

# Client admission profiles (plan §7.3).
CODEX_REQUIRED_FEATURES = ("responses", "streaming", "tools")
OPENCODE_REQUIRED_FEATURES = ("chat",)
AGENT_REQUIRED_FEATURES = ("chat",)

_SECRET_SCAN = (
    re.compile(r"sk-[a-z0-9_\-]{16,}", re.IGNORECASE),
    re.compile(r"(?i)bearer\s+[a-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|authorization)\s*[:=]\s*\S{6,}"),
)


class ClientConfigError(Exception):
    """Raised when a generated client config fails validation."""


def _contains_secret(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _SECRET_SCAN)


def _assert_no_secrets(text: str, client: str) -> None:
    if _contains_secret(text):
        raise ClientConfigError(
            f"{client} config rejected: credential-like content in output"
        )


def _production_providers(
    runtime_providers: Sequence[RuntimeProvider],
    confirmed_production_ids: Optional[Iterable[str]],
) -> List[RuntimeProvider]:
    """Filter to providers confirmed in BOTH runtime state and pool readback.

    ``confirmed_production_ids`` comes from the Pool Control production
    catalog readback (list_production()). When it is None (offline unit use
    with explicit consent), only the runtime state filter applies.
    """
    confirmed = (
        set(confirmed_production_ids) if confirmed_production_ids is not None else None
    )
    selected: List[RuntimeProvider] = []
    for rp in runtime_providers:
        if rp.actual_pool_status != ActualPoolStatus.PRODUCTION:
            continue
        if confirmed is not None and rp.provider_id not in confirmed:
            continue
        selected.append(rp)
    return selected


def _features_passing(rp: RuntimeProvider) -> Dict[str, bool]:
    pr = rp.protocol_result
    return {
        "chat": pr.chat == "pass",
        "responses": pr.responses == "pass",
        "streaming": pr.streaming == "pass",
        "tools": pr.tools == "pass",
    }


def _admits(rp: RuntimeProvider, required: Sequence[str]) -> bool:
    features = _features_passing(rp)
    return all(features[name] for name in required)


def _env_var_name(provider_id: str) -> str:
    """Stable env var NAME (not value) for a provider's credentials."""
    safe = re.sub(r"[^A-Z0-9_]", "_", provider_id.upper())
    return f"HUNTER_POOL_{safe}_KEY"


# ---------------------------------------------------------------------------
# Validation helpers (re-parse + re-validate, review 七)
# ---------------------------------------------------------------------------


def _validate_yaml_roundtrip(text: str, client: str) -> Dict[str, Any]:
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ClientConfigError(f"{client} config failed yaml round-trip") from exc
    if not isinstance(parsed, dict):
        raise ClientConfigError(f"{client} config did not parse to a mapping")
    return parsed


def _validate_json_roundtrip(text: str, client: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClientConfigError(f"{client} config failed json round-trip") from exc
    if not isinstance(parsed, dict):
        raise ClientConfigError(f"{client} config did not parse to an object")
    return parsed


def _validate_toml_roundtrip(text: str, client: str) -> Dict[str, Any]:
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ClientConfigError(f"{client} config failed toml round-trip") from exc
    return parsed


# ---------------------------------------------------------------------------
# Codex — config.toml with [model_providers.<id>], wire_api="responses"
# ---------------------------------------------------------------------------


def _toml_quote(value: str) -> str:
    out = ['"']
    for ch in str(value):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _toml_key(key: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", key or ""):
        return key
    return _toml_quote(key)


def generate_codex_config(
    runtime_providers: Sequence[RuntimeProvider],
    confirmed_production_ids: Optional[Iterable[str]] = None,
) -> str:
    """Generate Codex ``config.toml`` provider sections.

    Codex admission profile (plan §7.3): Responses + Streaming + Tools must
    all pass. Wire API is ``responses`` on the production proxy. Auth is an
    environment variable NAME only.
    """
    lines: List[str] = [
        "# Codex custom providers managed by hunter (FreeLLMPool production proxy).",
        "# Authentication references environment variable NAMES only.",
    ]
    included = 0
    for rp in _production_providers(runtime_providers, confirmed_production_ids):
        if not _admits(rp, CODEX_REQUIRED_FEATURES):
            continue
        included += 1
        key = _toml_key(rp.provider_id)
        lines.append("")
        lines.append(f"[model_providers.{key}]")
        lines.append(f"name = {_toml_quote(rp.provider_name or rp.provider_id)}")
        lines.append(
            f"base_url = {_toml_quote(f'{PRODUCTION_PROXY_BASE}/{rp.provider_id}/responses')}"
        )
        lines.append('wire_api = "responses"')
        lines.append(f"env_key = {_toml_quote(_env_var_name(rp.provider_id))}")
    text = "\n".join(lines) + "\n"
    _validate_toml_roundtrip(text, "codex")
    _assert_no_secrets(text, "codex")
    return text


# ---------------------------------------------------------------------------
# OpenCode — opencode.json provider block
# ---------------------------------------------------------------------------


def generate_opencode_config(
    runtime_providers: Sequence[RuntimeProvider],
    confirmed_production_ids: Optional[Iterable[str]] = None,
) -> str:
    """Generate OpenCode ``opencode.json`` provider configuration.

    OpenCode admission profile: the configured OpenAI-compatible profile
    requires a passing chat canary. Models list the declared capabilities.
    """
    providers: Dict[str, Any] = {}
    for rp in _production_providers(runtime_providers, confirmed_production_ids):
        if not _admits(rp, OPENCODE_REQUIRED_FEATURES):
            continue
        features = _features_passing(rp)
        options: Dict[str, Any] = {"baseURL": f"{PRODUCTION_PROXY_BASE}/{rp.provider_id}"}
        providers[rp.provider_id] = {
            "name": rp.provider_name or rp.provider_id,
            "npm": "@ai-sdk/openai-compatible",
            "options": options,
            "models": {
                f"{rp.provider_id}-default": {
                    "name": rp.provider_name or rp.provider_id,
                    "capabilities": [k for k, v in features.items() if v] or ["chat"],
                }
            },
        }
    payload = {"$schema": "https://opencode.ai/config.json", "provider": providers}
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    _validate_json_roundtrip(text, "opencode")
    _assert_no_secrets(text, "opencode")
    return text


# ---------------------------------------------------------------------------
# Generic OpenAI-compatible Agent — YAML
# ---------------------------------------------------------------------------


def generate_agent_config(
    runtime_providers: Sequence[RuntimeProvider],
    confirmed_production_ids: Optional[Iterable[str]] = None,
) -> str:
    """Generate a generic agent YAML config for the production proxy."""
    entries: List[Dict[str, Any]] = []
    for rp in _production_providers(runtime_providers, confirmed_production_ids):
        if not _admits(rp, AGENT_REQUIRED_FEATURES):
            continue
        features = _features_passing(rp)
        entries.append(
            {
                "id": rp.provider_id,
                "name": rp.provider_name or rp.provider_id,
                "endpoint": f"{PRODUCTION_PROXY_BASE}/{rp.provider_id}",
                "auth_env": _env_var_name(rp.provider_id),  # NAME only
                "capabilities": [k for k, v in features.items() if v],
            }
        )
    payload = {"providers": entries}
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    _validate_yaml_roundtrip(text, "agent")
    _assert_no_secrets(text, "agent")
    return text


def write_client_configs(
    directory: Path,
    runtime_providers: Sequence[RuntimeProvider],
    confirmed_production_ids: Optional[Iterable[str]] = None,
) -> Dict[str, Path]:
    """Generate, re-validate, and atomically write the three client configs."""
    directory.mkdir(parents=True, exist_ok=True)
    outputs = {
        "codex": directory / "codex-providers.toml",
        "opencode": directory / "opencode.json",
        "agent": directory / "agent-providers.yaml",
    }
    contents = {
        "codex": generate_codex_config(runtime_providers, confirmed_production_ids),
        "opencode": generate_opencode_config(runtime_providers, confirmed_production_ids),
        "agent": generate_agent_config(runtime_providers, confirmed_production_ids),
    }
    import os
    import tempfile

    for name, path in outputs.items():
        fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=f".{path.name}-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(contents[name].encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return outputs
