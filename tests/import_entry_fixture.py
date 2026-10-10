"""Isolated import oracle: attempts fail before any external side effect."""
import builtins
import hashlib
import importlib
import json
import os
from pathlib import Path
import socket
import sys
import threading


def main():
    root = Path(__file__).resolve().parents[1]
    scratch = Path(sys.argv[1]).resolve()
    os.chdir(scratch)
    sys.path.insert(0, str(root))
    before = {str(p.relative_to(scratch)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in scratch.rglob("*") if p.is_file()}
    threads = threading.enumerate()
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"dotenv", "openai", "tools", "agent", "clients", "investigation",
                                  "incident_store", "monitor", "remediation", "agent_logger"}:
            raise AssertionError("entry_imported_runtime_dependency:" + name)
        return original_import(name, *args, **kwargs)

    def audit(event, args):
        if event.startswith("socket.") and event in {
            "socket.connect", "socket.bind", "socket.getaddrinfo", "socket.sendto",
        }:
            raise AssertionError("entry_attempted_network:" + event)
        if event in {"os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.system", "subprocess.Popen"}:
            raise AssertionError("entry_attempted_mutation:" + event)
        if event == "open":
            _, mode, flags = args
            if (mode and any(c in mode for c in "wax+")) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
                raise AssertionError("entry_attempted_file_write")

    def forbidden_thread(*args, **kwargs):
        raise AssertionError("entry_attempted_thread")

    builtins.__import__ = guarded_import
    threading.Thread.start = forbidden_thread
    sys.addaudithook(audit)
    mode = sys.argv[2] if len(sys.argv) > 2 else "imports"
    if mode == "probe-network":
        socket.socket().connect(("127.0.0.1", 1))
    elif mode == "probe-thread":
        threading.Thread(target=lambda: None).start()
    elif mode == "probe-write":
        (scratch / "must-not-exist").write_text("unsafe")
    elif mode == "probe-model":
        __import__("openai")
    modules = [importlib.import_module(name) for name in (
        "main", "runtime_cli", "remediation_cli", "scripts.validate_durable_runtime", "scripts.validate_multi_signal",
    )]
    assert all(callable(module.main) for module in modules)
    assert threading.enumerate() == threads
    after = {str(p.relative_to(scratch)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in scratch.rglob("*") if p.is_file()}
    assert before == after
    print(json.dumps({"imports": [m.__name__ for m in modules], "unchanged_files": len(before), "threads": len(threads)}))


if __name__ == "__main__":
    main()
