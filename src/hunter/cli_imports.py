"""CLI import wiring for Stage 2 subcommands.

Registers ``run-stage-two`` (flat) plus the nested ``pool approve`` and
``runtime status`` parsers. Argument parsing stays thin (no business logic);
every handler delegates to a worker in :mod:`hunter.runtime.stage2`.
"""

from __future__ import annotations

import os


def _resolve_data_dir(args) -> "Path":  # noqa: ANN001
    from pathlib import Path

    if getattr(args, "data_dir", None):
        return Path(args.data_dir)
    from .config import load_settings

    settings = load_settings()
    return Path(settings["paths"]["data_dir"])


def _handle_run_stage_two(args) -> int:  # noqa: ANN001
    import json

    from .pool_api import PoolControlClient
    from .runtime.stage2 import Stage2Runner

    data_dir = _resolve_data_dir(args)
    base_url = os.environ.get("HUNTER_POOL_CONTROL_URL", "")
    token = os.environ.get("HUNTER_POOL_CONTROL_TOKEN", "")
    if not base_url or not token:
        raise RuntimeError("pool control URL and token are required")
    pool_control = PoolControlClient(base_url, token)
    runner = Stage2Runner(data_dir=data_dir, pool_control=pool_control)
    summary = runner.run()
    print(json.dumps(summary.to_dict(), indent=2))
    if summary.skipped_locked:
        return 3  # documented skip exit code
    if summary.suspend_failed or summary.pool_runtime_split:
        return 1
    return 0


def _handle_pool_approve(args) -> int:  # noqa: ANN001
    import json

    from .runtime.stage2 import approve_provider

    data_dir = _resolve_data_dir(args)
    result = approve_provider(
        args.provider_id,
        args.expected_revision,
        data_dir=data_dir,
        approved_by=args.approved_by,
    )
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


def _handle_pool_control_status(args) -> int:  # noqa: ANN001
    """Sanitized pool/service status via PoolControlClient (no keys)."""
    import json

    from .pool_api import PoolControlClient

    base_url = os.environ.get("HUNTER_POOL_CONTROL_URL", "")
    token = os.environ.get("HUNTER_POOL_CONTROL_TOKEN", "")
    if not base_url or not token:
        raise RuntimeError("pool control URL and token are required")
    pc = PoolControlClient(base_url, token)
    status = pc.pool_status()
    print(json.dumps(status, indent=2))
    return 0


def _handle_pool_suspend(args) -> int:  # noqa: ANN001
    """Suspend a provider from the production pool via PoolControlClient."""
    import json

    from .pool_api import PoolControlClient

    base_url = os.environ.get("HUNTER_POOL_CONTROL_URL", "")
    token = os.environ.get("HUNTER_POOL_CONTROL_TOKEN", "")
    if not base_url or not token:
        raise RuntimeError("pool control URL and token are required")
    pc = PoolControlClient(base_url, token)
    result = pc.suspend(args.provider_id)
    print(json.dumps(result, indent=2))
    return 0 if result.get("suspended") else 1


def _handle_pool_stop(args) -> int:  # noqa: ANN001
    """Halt production containment via PoolControlClient."""
    import json

    from .pool_api import PoolControlClient

    base_url = os.environ.get("HUNTER_POOL_CONTROL_URL", "")
    token = os.environ.get("HUNTER_POOL_CONTROL_TOKEN", "")
    if not base_url or not token:
        raise RuntimeError("pool control URL and token are required")
    pc = PoolControlClient(base_url, token)
    result = pc.stop_production()
    print(json.dumps(result, indent=2))
    return 0


def _handle_runtime_status(args) -> int:  # noqa: ANN001
    import json

    from .runtime.stage2 import runtime_status

    rows = runtime_status(_resolve_data_dir(args))
    print(json.dumps(rows, indent=2))
    return 0


def register_stage_two_parsers(subparsers) -> None:  # noqa: ANN001
    """Attach the Stage 2 CLI surface to the hunter parser."""
    from .cli import register_subcommand

    run_two = subparsers.add_parser(
        "run-stage-two",
        help="run the full stage-two lifecycle (lock→reconcile→suspend-first→import→checks→promote→verify)",
    )
    run_two.add_argument("--data-dir", default=None, help="data directory")
    run_two.set_defaults(handler=_handle_run_stage_two)

    pool = subparsers.add_parser("pool", help="pool control operations")
    pool_sub = pool.add_subparsers(dest="pool_command", metavar="ACTION")
    approve = pool_sub.add_parser(
        "approve", help="approve a provider with a registry revision guard"
    )
    approve.add_argument("--provider-id", required=True)
    approve.add_argument("--expected-revision", type=int, required=True)
    approve.add_argument("--approved-by", default="user")
    approve.add_argument("--data-dir", default=None, help="data directory")
    approve.set_defaults(handler=_handle_pool_approve)

    control_status = pool_sub.add_parser(
        "control-status",
        help="show sanitized pool/service state (no keys, no full upstream responses)",
    )
    control_status.add_argument("--data-dir", default=None, help="data directory")
    control_status.set_defaults(handler=_handle_pool_control_status)

    suspend_pool = pool_sub.add_parser(
        "suspend",
        help="suspend a provider from the production pool (requires --confirm)",
    )
    suspend_pool.add_argument("--provider-id", required=True)
    suspend_pool.add_argument("--confirm", required=True, help="literal SUSPEND")
    suspend_pool.add_argument("--data-dir", default=None, help="data directory")
    suspend_pool.set_defaults(handler=_handle_pool_suspend)

    stop_pool = pool_sub.add_parser(
        "stop",
        help="halt the production pool (requires --confirm STOP)",
    )
    stop_pool.add_argument("--confirm", required=True, help="literal STOP")
    stop_pool.add_argument("--data-dir", default=None, help="data directory")
    stop_pool.set_defaults(handler=_handle_pool_stop)

    pool.set_defaults(handler=_pool_usage)

    runtime = subparsers.add_parser("runtime", help="runtime store operations")
    runtime_sub = runtime.add_subparsers(dest="runtime_command", metavar="ACTION")
    status = runtime_sub.add_parser("status", help="list runtime providers (sanitized; distinct revisions)")
    status.add_argument("--data-dir", default=None, help="data directory")
    status.set_defaults(handler=_handle_runtime_status)
    runtime.set_defaults(handler=_runtime_usage)


def _pool_usage(args) -> int:  # noqa: ANN001, ARG001
    print("usage: hunter pool approve|control-status|suspend|stop ...")
    print("  hunter pool approve     --provider-id ID --expected-revision N")
    print("  hunter pool control-status")
    print("  hunter pool suspend     --provider-id ID --confirm SUSPEND")
    print("  hunter pool stop        --confirm STOP")
    return 2


def _runtime_usage(args) -> int:  # noqa: ANN001, ARG001
    print("usage: hunter runtime status [--data-dir DIR]")
    return 2


def load_cli_imports() -> None:
    """Import hook kept for hunter.cli compatibility."""
    return None


__all__ = ["load_cli_imports", "register_stage_two_parsers"]
