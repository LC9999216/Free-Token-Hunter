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
    from pathlib import Path

    from .pool_control import PoolControl
    from .runtime.stage2 import Stage2Runner

    data_dir = _resolve_data_dir(args)
    base = Path(
        os.environ.get("HUNTER_POOL_BASE_DIR") or (Path.home() / ".hunter-pool")
    )
    staging = Path(args.staging_dir) if args.staging_dir else base / "staging"
    production = Path(args.production_dir) if args.production_dir else base / "production"
    repo_root = Path(__file__).resolve().parents[2]
    pool_control = PoolControl(staging, production, hunter_root=repo_root)
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
    run_two.add_argument("--staging-dir", default=None, help="pool staging directory")
    run_two.add_argument("--production-dir", default=None, help="pool production directory")
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
    pool.set_defaults(handler=_pool_usage)

    runtime = subparsers.add_parser("runtime", help="runtime store operations")
    runtime_sub = runtime.add_subparsers(dest="runtime_command", metavar="ACTION")
    status = runtime_sub.add_parser("status", help="list runtime providers (sanitized)")
    status.add_argument("--data-dir", default=None, help="data directory")
    status.set_defaults(handler=_handle_runtime_status)
    runtime.set_defaults(handler=_runtime_usage)


def _pool_usage(args) -> int:  # noqa: ANN001, ARG001
    print("usage: hunter pool approve --provider-id ID --expected-revision N")
    return 2


def _runtime_usage(args) -> int:  # noqa: ANN001, ARG001
    print("usage: hunter runtime status")
    return 2


def load_cli_imports() -> None:
    """Import hook kept for hunter.cli compatibility."""
    return None


__all__ = ["load_cli_imports", "register_stage_two_parsers"]
