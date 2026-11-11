"""Start the existing W2 API and Vite for a fresh, human-operated W3 run.

Never submits a recovery request, approval or execution. Ctrl+C / experiment
STOP closes only the owned API/application and this launcher's Vite process.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from remediation.windows_process import ProcessHandle
from remediation.state import RemediationError
from scripts.w2_demo import main as demo_main


def main():
    parser = argparse.ArgumentParser(description="W3 人工浏览器验收：新实验、三种操作身份、300 秒证据。")
    parser.add_argument("--port", type=int, default=18330)
    parser.add_argument("--app-port", type=int, default=18430)
    args = parser.parse_args()
    if (args.port == args.app_port or 5173 in {args.port, args.app_port}
            or not all(1024 <= port <= 65535 for port in (args.port, args.app_port))):
        parser.error("distinct_loopback_ports_required")
    node = shutil.which("node")
    vite = ROOT / "frontend/node_modules/vite/bin/vite.js"
    if not node or not vite.is_file():
        parser.error("existing_frontend_node_and_dependencies_required")
    with socket.socket() as listener:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 5173))
    session = Path(tempfile.mkdtemp(prefix="incident-w3-human-"))
    experiment = session / "experiment"
    environment = dict(os.environ, VITE_API_BASE_URL=f"http://127.0.0.1:{args.port}")
    arguments = [str(Path(node).resolve()), str(vite), "--host", "127.0.0.1"]
    with (session / "frontend.log").open("wb") as output:
        process = subprocess.Popen(
            arguments, cwd=ROOT / "frontend", env=environment, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    snapshot = None
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                with ProcessHandle(process.pid) as handle:
                    candidate = handle.snapshot()
                if (Path(candidate["cwd"]).resolve() == ROOT / "frontend"
                        and Path(candidate["exe"]).resolve() == Path(node).resolve()
                        and candidate["argv"] == arguments):
                    snapshot = candidate
                    break
            except (OSError, RemediationError):
                if process.poll() is not None:
                    raise
            time.sleep(0.02)
        if snapshot is None:
            raise RuntimeError("frontend_process_identity_unconfirmed")
        (session / "frontend-ownership.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        if process.poll() is not None:
            raise RuntimeError("frontend_start_failed")
        print(json.dumps({
            "human_acceptance": "pending", "experiment": str(experiment),
            "workbench": "http://127.0.0.1:5173/remediations/rem-w2-deploy-b",
            "credentials_file": str(experiment / "operators.json"),
            "stop_file": str(experiment / "STOP"),
        }, ensure_ascii=False), flush=True)
        demo_main(["--manual-approval", "--root", str(experiment),
                   "--port", str(args.port), "--app-port", str(args.app_port)])
    finally:
        cleanup = {"pid": process.pid, "stopped": process.poll() is not None}
        if process.poll() is None and snapshot is not None:
            with ProcessHandle(process.pid) as handle:
                if handle.snapshot() != snapshot:
                    raise RuntimeError("frontend_process_identity_changed_refusing_stop")
            process.terminate()  # Original Popen HANDLE, never an unrelated PID.
            process.wait(timeout=5)
            cleanup["stopped"] = True
        (session / "frontend-cleanup.json").write_text(json.dumps(cleanup), encoding="utf-8")


if __name__ == "__main__":
    main()
