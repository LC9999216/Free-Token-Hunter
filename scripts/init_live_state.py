"""Initialize the external live-state directory for Stage 2 validation.

Creates:
  D:\AI\Free token\Free Token Hunter-stage2-live-state/
    data/            — Registry, Candidate, Evidence copies (from repo data/)
    pool/staging/    — Pool Control staging TOML directories
    pool/production/ — Pool Control production TOML directories
    clients/         — Client config output directory

Never modifies repository data/*.json.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

EXTERNAL_STATE = Path("D:/AI/Free token/Free Token Hunter-stage2-live-state")


def init(worktree_root: Path) -> dict[str, str]:
    """Create or verify the external state directory structure."""
    EXTERNAL_STATE.mkdir(parents=True, exist_ok=True)

    # Pool TOML directories
    (EXTERNAL_STATE / "pool" / "staging").mkdir(parents=True, exist_ok=True)
    (EXTERNAL_STATE / "pool" / "production").mkdir(parents=True, exist_ok=True)

    # Live data directory
    live_data = EXTERNAL_STATE / "data"
    live_data.mkdir(parents=True, exist_ok=True)

    # Client config output
    (EXTERNAL_STATE / "clients").mkdir(parents=True, exist_ok=True)

    # Copy repository data files if not already present
    copied = []
    for name in ("providers.json", "candidates.json", "evidence.json", "history.jsonl"):
        src = worktree_root / "data" / name
        dst = live_data / name
        if src.is_file() and not dst.is_file():
            shutil.copy2(str(src), str(dst))
            copied.append(name)
        elif src.is_file() and dst.is_file():
            # Already exists — verify hash
            import hashlib
            src_hash = hashlib.sha256(src.read_bytes()).hexdigest()[:16]
            dst_hash = hashlib.sha256(dst.read_bytes()).hexdigest()[:16]
            if src_hash != dst_hash:
                shutil.copy2(str(src), str(dst))
                copied.append(f"{name} (refreshed)")

    return {
        "external_state": str(EXTERNAL_STATE),
        "data": str(live_data),
        "pool_staging": str(EXTERNAL_STATE / "pool" / "staging"),
        "pool_production": str(EXTERNAL_STATE / "pool" / "production"),
        "clients": str(EXTERNAL_STATE / "clients"),
        "copied": str(copied),
    }


if __name__ == "__main__":
    import json
    worktree = Path(__file__).resolve().parents[1]
    result = init(worktree)
    print(json.dumps(result, indent=2))
