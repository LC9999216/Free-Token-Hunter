"""Tests for the process boundary that owns Pool Control and the proxy."""

from __future__ import annotations

from pathlib import Path

import pytest

from hunter.pool_service import PoolService, PoolServiceError, build_service, main
from hunter.runtime.locks import LockHeldError


class FakeServiceSupervisor:
    def __init__(self, provider_ids: list[str]):
        self._provider_ids = set(provider_ids)
        self.reload_calls = 0
        self.halt_calls = 0

    def reload(self) -> None:
        self.reload_calls += 1

    def halt(self) -> None:
        self.halt_calls += 1
        self._provider_ids.clear()

    def running_provider_ids(self) -> set[str]:
        return set(self._provider_ids)


class FakePoolControl:
    def __init__(self, production_halted: bool):
        self.production_halted = production_halted


class FakeControlServer:
    def __init__(self):
        self.serve_calls = 0
        self.close_calls = 0

    def serve_forever(self) -> None:
        self.serve_calls += 1

    def close(self) -> None:
        self.close_calls += 1


def test_service_rejects_missing_control_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("HUNTER_POOL_CONTROL_TOKEN", raising=False)
    with pytest.raises(PoolServiceError, match="control_token_required"):
        build_service(
            staging_dir=tmp_path / "staging",
            production_dir=tmp_path / "production",
            host="127.0.0.1",
            port=8091,
            proxy_port=8080,
        )


def test_service_does_not_start_proxy_while_halted() -> None:
    supervisor = FakeServiceSupervisor([])
    server = FakeControlServer()
    service = PoolService(
        pool_control=FakePoolControl(production_halted=True),
        proxy_supervisor=supervisor,
        control_server=server,
    )
    service.run()
    assert supervisor.reload_calls == 0
    assert supervisor.halt_calls == 1
    assert server.close_calls == 1


def test_service_cli_sanitizes_unexpected_proxy_startup_error(
    monkeypatch, capsys
) -> None:
    class RaisingSupervisor(FakeServiceSupervisor):
        def reload(self) -> None:
            raise RuntimeError("synthetic secret-like upstream detail")

    service = PoolService(
        pool_control=FakePoolControl(production_halted=False),
        proxy_supervisor=RaisingSupervisor([]),
        control_server=FakeControlServer(),
    )
    monkeypatch.setattr("hunter.pool_service.build_service", lambda **kwargs: service)
    assert main(["serve"]) == 1
    assert "synthetic" not in capsys.readouterr().out


def test_resume_refuses_to_clear_latch_while_service_lock_is_held(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    state_path = tmp_path / "production" / "control_state.json"
    state_path.parent.mkdir()
    state_path.write_text(
        '{"production_halted":true,"reason":"test"}', encoding="utf-8"
    )
    monkeypatch.setenv("HUNTER_POOL_CONTROL_TOKEN", "test-control-token")

    class AlwaysHeldLock:
        def __init__(self, path: Path):
            self.path = path

        def acquire(self, *args, **kwargs) -> None:
            raise LockHeldError("test lock held")

        def release(self) -> None:
            return

    monkeypatch.setattr(
        "hunter.pool_service.ProcessFileLock", AlwaysHeldLock, raising=False
    )
    code = main(
        [
            "resume",
            "--confirm",
            "RESUME",
            "--staging-dir",
            str(tmp_path / "staging"),
            "--production-dir",
            str(tmp_path / "production"),
        ]
    )
    assert code == 1
    assert "pool_control_service_running" in capsys.readouterr().out
    assert '"production_halted":true' in state_path.read_text(encoding="utf-8")
