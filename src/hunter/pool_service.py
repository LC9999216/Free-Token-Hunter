"""Dedicated Pool Control service that owns TOML, keys, and the proxy."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .pool_api import PoolControlServer
from .pool_control import FreellmpoolProbeRunner, PoolControl, PoolControlError
from .pool_proxy import FreellmpoolProxySupervisor, ProxySupervisor
from .runtime.locks import LockHeldError, ProcessFileLock


class PoolServiceError(Exception):
    """Safe service startup failure without credential material."""


@dataclass
class PoolService:
    pool_control: PoolControl
    proxy_supervisor: ProxySupervisor
    control_server: PoolControlServer
    service_lock: Optional[ProcessFileLock] = None

    def run(self) -> None:
        if self.service_lock is not None:
            try:
                self.service_lock.acquire(reentrant=False)
            except LockHeldError as exc:
                raise PoolServiceError("pool_control_service_running") from exc
        try:
            if not self.pool_control.production_halted:
                self.proxy_supervisor.reload()
            self.control_server.serve_forever()
        finally:
            self.proxy_supervisor.halt()
            self.control_server.close()
            if self.service_lock is not None:
                self.service_lock.release()


def _build_pool_components(
    *,
    staging_dir: Path,
    production_dir: Path,
    host: str,
    proxy_port: int,
) -> tuple[PoolControl, ProxySupervisor, str]:
    token = os.environ.get("HUNTER_POOL_CONTROL_TOKEN", "")
    if not token:
        raise PoolServiceError("control_token_required")
    if host != "127.0.0.1":
        raise PoolServiceError("loopback_host_required")
    staging_dir = Path(staging_dir)
    production_dir = Path(production_dir)
    os.environ["FREELLMPOOL_CONFIG"] = str(production_dir / "providers.toml")
    os.environ["FREELLMPOOL_CONFIG_FILE"] = str(production_dir / "config.toml")
    supervisor = FreellmpoolProxySupervisor(
        production_dir / "providers.toml",
        production_dir / "config.toml",
        host="127.0.0.1",
        port=proxy_port,
        proxy_key=os.environ.get("FREELLMPOOL_PROXY_KEY") or None,
    )
    control = PoolControl(
        staging_dir,
        production_dir,
        probe_runner=FreellmpoolProbeRunner(),
        proxy_supervisor=supervisor,
    )
    return control, supervisor, token


def build_service(
    *,
    staging_dir: Path,
    production_dir: Path,
    host: str,
    port: int,
    proxy_port: int,
) -> PoolService:
    control, supervisor, token = _build_pool_components(
        staging_dir=staging_dir,
        production_dir=production_dir,
        host=host,
        proxy_port=proxy_port,
    )
    FreellmpoolProbeRunner().check_version()
    server = PoolControlServer(control, token, host=host, port=port)
    return PoolService(
        pool_control=control,
        proxy_supervisor=supervisor,
        control_server=server,
        service_lock=ProcessFileLock(production_dir / ".pool-control-service.lock"),
    )


def _default_pool_dir() -> Path:
    return Path.home() / ".hunter-pool"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hunter-pool-control",
        description="Run the isolated loopback Pool Control service.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("serve", "resume"):
        child = subparsers.add_parser(command)
        child.add_argument("--staging-dir", type=Path, default=_default_pool_dir() / "staging")
        child.add_argument(
            "--production-dir", type=Path, default=_default_pool_dir() / "production"
        )
        child.add_argument("--host", default="127.0.0.1")
        child.add_argument("--port", type=int, default=8091)
        child.add_argument("--proxy-port", type=int, default=8080)
        if command == "resume":
            child.add_argument("--confirm", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "resume":
            if args.confirm != "RESUME":
                raise PoolServiceError("resume_confirmation_required")
            control, _, _ = _build_pool_components(
                staging_dir=args.staging_dir,
                production_dir=args.production_dir,
                host=args.host,
                proxy_port=args.proxy_port,
            )
            service_lock = ProcessFileLock(
                args.production_dir / ".pool-control-service.lock"
            )
            try:
                service_lock.acquire(reentrant=False)
            except LockHeldError as exc:
                raise PoolServiceError("pool_control_service_running") from exc
            try:
                control.resume_production()
            finally:
                service_lock.release()
            print("production halt latch cleared; start serve to reload the proxy")
            return 0
        service = build_service(
            staging_dir=args.staging_dir,
            production_dir=args.production_dir,
            host=args.host,
            port=args.port,
            proxy_port=args.proxy_port,
        )
        service.run()
        return 0
    except (PoolControlError, PoolServiceError) as exc:
        print(str(exc))
        return 1
    except Exception:  # noqa: BLE001 - never expose proxy or upstream details
        print("pool_service_start_failed")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["PoolService", "PoolServiceError", "build_service", "main"]
