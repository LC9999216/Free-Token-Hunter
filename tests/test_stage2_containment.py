"""Offline real process contention tests: fixture Pool Control, no upstream calls."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

import pytest

from hunter.pool_api import PoolControlClient, PoolControlServer
from hunter.registry.schema import ProviderStatus
from hunter.runtime.outbox import OutboxStore
from hunter.runtime.stage2 import Stage2Runner
from tests.test_stage2_runner import _seed, _drive_to_approval, _registry_provider


@pytest.mark.parametrize("suspend_fails", [False, True])
def test_external_drain_lock_does_not_gate_containment(outside_repo_tmp_path, suspend_fails):
    data_dir, registry, evidence, pool = _seed(outside_repo_tmp_path / "case", hunter_root=None)
    _drive_to_approval(data_dir, pool)
    pool.promote("acme")
    registry.upsert_provider(_registry_provider(status=ProviderStatus.NOT_FREE), reason="withdrawn", source_metadata={"fixture": True})
    if suspend_fails:
        pool.suspend = lambda pid: {"suspended": False, "error": "fixture_failure"}

    script = '''
import sys
from pathlib import Path
from hunter.runtime.outbox import OutboxStore
with_path = Path(sys.argv[1])
store = OutboxStore(with_path)
try:
    print("LOCKED_READY", flush=True)
    assert sys.stdin.readline().strip() == "release"
finally:
    store.close()
'''
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    child = subprocess.Popen([sys.executable, "-B", "-c", script, str(data_dir / "notification_outbox.json")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    ready = queue.Queue()
    reader = threading.Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True)
    reader.start()
    server = PoolControlServer(pool, "offline-control-fixture")
    server.start_background()
    try:
        assert ready.get(timeout=10).strip() == "LOCKED_READY"
        runner = Stage2Runner(data_dir=data_dir, registry=registry, evidence_store=evidence, pool_control=PoolControlClient(server.url, "offline-control-fixture"))
        summary = runner.run()
        if suspend_fails:
            assert summary.suspend_failed == ["acme"]
            assert pool.production_halted
        else:
            assert summary.suspended == ["acme"]
            assert pool.list_production() == []
            assert not pool.production_halted
        intent_path = data_dir / "suspension_alert_intents.json"
        assert "acme" in json.loads(intent_path.read_text(encoding="utf-8"))
        _, errors = child.communicate("release\n", timeout=10)
        assert child.returncode == 0, errors
        # Replay pending intent after release without another upstream call.
        from hunter.runtime.stage2 import Stage2Summary
        runner._recover_suspension_alerts(Stage2Summary())
        assert json.loads(intent_path.read_text(encoding="utf-8")) == {}
        outbox = OutboxStore(data_dir / "notification_outbox.json")
        try:
            messages = outbox.pending()
            assert len(messages) == 1
            assert messages[0].event_type == "PRODUCTION_BLOCKED"
            assert "requested" in messages[0].title
        finally:
            outbox.close()
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
        server.shutdown()
        reader.join(timeout=2)
