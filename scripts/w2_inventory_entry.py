"""Packaged W2 entry point. Started only from a verified extracted artifact."""
import hashlib
import json
import os
from pathlib import Path
import sys


def main():
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "release.json").read_text(encoding="utf-8"))
    port, instance, artifact, configuration = sys.argv[1:]
    # -I -S excludes site-packages; all third party code comes from the artifact.
    sys.path[:0] = [str(root), str(root / "vendor")]
    os.environ["APP_COMMIT_SHA"] = manifest["source_commit"]
    os.environ["SIMULATE_INVENTORY_ERROR"] = str(manifest["inventory_error"])
    os.environ["SIMULATE_SLOW_REQUEST"] = "false"

    def deny_external_network(event, arguments):
        if event == "socket.connect":
            raise RuntimeError("isolated_inventory_forbids_outbound_effects")

    sys.addaudithook(deny_external_network)
    # Windows platform detection in prometheus_client can query the OS version
    # during import. After these pinned imports, also forbid process creation.
    import inventory_app
    from werkzeug.serving import make_server

    def deny_external_processes(event, arguments):
        if event in {"subprocess.Popen", "os.system"}:
            raise RuntimeError("isolated_inventory_forbids_external_processes")

    sys.addaudithook(deny_external_processes)

    loaded_sources = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in ("inventory_app.py", "http_observability.py")
    }
    original_version = inventory_app.version

    def attested_version():
        body, status = original_version()
        return body | {
            "release_id": manifest["release_id"],
            "artifact_sha256": artifact,
            "configuration_digest": configuration,
            "instance_id": instance,
            "pid": os.getpid(),
            "source_files": loaded_sources,
        }, status

    inventory_app.app.view_functions["version"] = attested_version
    # make_server binds once, without a reloader, subprocess or external listener.
    make_server("127.0.0.1", int(port), inventory_app.app).serve_forever()


if __name__ == "__main__":
    main()
