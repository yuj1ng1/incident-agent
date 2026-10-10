"""Real CLI/SQLite process with an isolated Prometheus response fixture."""
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clients.prometheus_client import PrometheusClient
from runtime_cli import main


if __name__ == "__main__":
    directory, session_id, owner = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    (directory / ("ready-reopen-" + owner)).write_text("ready")
    deadline = time.monotonic() + 20
    while not (directory / "release-reopen").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("reopen_barrier_timeout")
        time.sleep(0.02)
    snapshot = json.loads((directory / "live-alert-fixture.json").read_text(encoding="utf-8"))
    with patch.object(PrometheusClient, "get_alerts", return_value=snapshot):
        raise SystemExit(main([
            "--store", str(directory / "runtime.sqlite3"),
            "--incidents", str(directory / "incidents.json"),
            "--remediations", str(directory / "remediation.json"),
            "reopen", session_id, "--actor", "process-operator", "--reason", "configuration corrected",
            "--command-id", "same-reopen-command",
        ]))
