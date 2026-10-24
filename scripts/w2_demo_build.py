"""Deterministic inventory bundles from exact Git blobs and installed packages.

Run with prometheus-demo's interpreter. No install, network, database or Git write.
Both releases share a source commit; B is an explicitly changed configuration.
"""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import zipfile

PACKAGES = (
    "Flask", "Werkzeug", "Jinja2", "MarkupSafe", "click", "itsdangerous",
    "blinker", "prometheus-client",
)


def build(repo, output):
    repo, output = Path(repo).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, timeout=5,
    ).decode().strip()
    files = {
        name: subprocess.check_output(
            ["git", "show", f"{commit}:{name}"], cwd=repo, timeout=5,
        ) for name in ("inventory_app.py", "http_observability.py")
    }
    files["launch.py"] = Path(__file__).with_name(
        "w2_inventory_entry.py"
    ).read_bytes()
    versions = {}
    for package in PACKAGES:
        distribution = importlib.metadata.distribution(package)
        versions[package] = distribution.version
        for item in distribution.files:
            if (".." in item.parts or "__pycache__" in item.parts
                    or item.suffix in {".pyc", ".pyo"}):
                continue
            source = Path(distribution.locate_file(item))
            if source.is_file():
                files["vendor/" + item.as_posix()] = source.read_bytes()
    interpreter = Path(sys._base_executable).resolve()
    results = {}
    for release_id, error in (("A", False), ("B", True)):
        manifest = {
            "schema_version": 1,
            "service": "inventory-service",
            "release_id": release_id,
            "source_commit": commit,
            "inventory_error": error,
            "packages": versions,
            "python_version": sys.version,
            "interpreter": str(interpreter),
            "interpreter_sha256": hashlib.sha256(interpreter.read_bytes()).hexdigest(),
            "files": {name: hashlib.sha256(data).hexdigest()
                      for name, data in sorted(files.items())},
        }
        release_files = files | {
            "release.json": json.dumps(manifest, sort_keys=True).encode(),
        }
        path = output / f"inventory-{release_id}.zip"
        with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
            for name, data in sorted(release_files.items()):
                info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100444 << 16
                bundle.writestr(info, data)
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        immutable_path = output / f"inventory-{release_id}-{checksum}.zip"
        path.rename(immutable_path)
        results[release_id] = {
            "path": str(immutable_path),
            "sha256": checksum,
            "manifest": manifest,
        }
    (output / "build.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8",
    )
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("repo")
    parser.add_argument("output")
    args = parser.parse_args()
    build(args.repo, args.output)
