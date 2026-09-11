"""CLI import wiring.

Subcommand handlers are registered directly in :mod:`hunter.cli`'s
``build_parser`` (``import-seed``, ``discover``, ``collect-evidence``,
``pipeline``). This module keeps the load hook used by ``hunter.cli`` so the
console script can degrade gracefully if an adapter import fails. No business
logic lives in argument parsing.
"""

from __future__ import annotations


def load_cli_imports() -> None:
    """Import any task modules that register subcommands at import time.

    The current subcommands are self-contained in hunter.cli; this hook stays
    for future adapters that register themselves.
    """
    return None


__all__ = ["load_cli_imports"]
