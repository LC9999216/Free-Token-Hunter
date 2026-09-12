"""Client config generators — OpenCode, Codex, Agent YAML from production pool (Stage 5).

Generates configuration files for downstream clients that consume the
FreeLLMPool. Only providers with actual_pool_status == PRODUCTION are
included. No secrets are written to client configs (base_url only,
no api_key, no tokens).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from hunter.runtime.models import RuntimeProvider


def _providers_for_config(runtime_providers: List[RuntimeProvider]) -> List[RuntimeProvider]:
    """Filter to production providers only."""
    return [rp for rp in runtime_providers if rp.actual_pool_status == "PRODUCTION"]


def generate_opencode_config(runtime_providers: List[RuntimeProvider]) -> str:
    """Generate OpenCode providers.yaml content.

    OpenCode format:
    """
    providers: list[dict[str, Any]] = []
    for rp in _providers_for_config(runtime_providers):
        entry: dict[str, Any] = {
            "id": rp.provider_id,
            "name": rp.provider_name or rp.provider_id,
        }
        # Only add fields that have values
        if rp.protocol_result and rp.protocol_result.chat == "pass":
            entry["chat"] = True
        if rp.protocol_result and rp.protocol_result.responses == "pass":
            entry["responses"] = True
        if rp.protocol_result and rp.protocol_result.streaming == "pass":
            entry["streaming"] = True
        if rp.protocol_result and rp.protocol_result.tools == "pass":
            entry["tools"] = True
        providers.append(entry)

    return _yaml_dump({"providers": providers})


def generate_codex_config(runtime_providers: List[RuntimeProvider]) -> str:
    """Generate Codex codex-providers.yaml content.

    Codex format with provider section and model mapping.
    """
    providers: list[dict[str, Any]] = []
    for rp in _providers_for_config(runtime_providers):
        entry: dict[str, Any] = {
            "id": rp.provider_id,
            "name": rp.provider_name or rp.provider_id,
            "base_url": f"http://127.0.0.1:8080/v1/{rp.provider_id}",
            "active": True,
        }
        if rp.protocol_result and rp.protocol_result.chat == "pass":
            entry["capabilities"] = ["chat"]
        if rp.protocol_result and rp.protocol_result.streaming == "pass":
            if "capabilities" not in entry:
                entry["capabilities"] = []
            entry["capabilities"].append("streaming")
        providers.append(entry)

    return _yaml_dump({"providers": providers})


def generate_agent_config(runtime_providers: List[RuntimeProvider]) -> str:
    """Generate Agent agent-providers.yaml content.

    Agent format with friendly name, supported actions, and endpoint.
    """
    providers: list[dict[str, Any]] = []
    for rp in _providers_for_config(runtime_providers):
        entry: dict[str, Any] = {
            "id": rp.provider_id,
            "name": rp.provider_name or rp.provider_id,
            "endpoint": f"http://127.0.0.1:8080/v1/{rp.provider_id}",
        }
        actions = []
        if rp.protocol_result and rp.protocol_result.chat == "pass":
            actions.append("chat")
        if rp.protocol_result and rp.protocol_result.responses == "pass":
            actions.append("response")
        if rp.protocol_result and rp.protocol_result.streaming == "pass":
            actions.append("streaming")
        if rp.protocol_result and rp.protocol_result.tools == "pass":
            actions.append("tool_use")
        if actions:
            entry["actions"] = actions
        providers.append(entry)

    return _yaml_dump({"providers": providers})


def _yaml_dump(data: Any) -> str:
    """Simple YAML serializer (avoids pyyaml dependency)."""
    lines: list[str] = []
    _serialize_yaml(data, lines, 0)
    return "\n".join(lines) + "\n"


def _serialize_yaml(data: Any, lines: list[str], indent: int) -> None:
    prefix = "  " * indent
    if isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, (dict, list)) and val:
                lines.append(f"{prefix}{key}:")
                _serialize_yaml(val, lines, indent + 1)
            elif isinstance(val, bool):
                lines.append(f"{prefix}{key}: {str(val).lower()}")
            elif val is None:
                lines.append(f"{prefix}{key}: null")
            else:
                lines.append(f"{prefix}{key}: {val}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                lines.append(f"{prefix}-")
                _serialize_yaml(item, lines, indent + 1)
            elif isinstance(item, bool):
                lines.append(f"{prefix}- {str(item).lower()}")
            else:
                lines.append(f"{prefix}- {_yaml_escape(item)}")
    else:
        lines.append(f"{prefix}{_yaml_escape(data)}")


def _yaml_escape(val: Any) -> str:
    s = str(val)
    if ":" in s or "#" in s or s.startswith(("-", "?", "&", "*", "!", "|", ">", "'", '"')) or s == "":
        return f'"{s}"'
    return s
