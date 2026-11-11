"""Deterministic inventory bundles from exact Git blobs and installed packages.

Run with prometheus-demo's interpreter. No install, network, database or Git write.
Both releases share a source commit; B is an explicitly changed configuration.
"""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile

PACKAGES = (
    "Flask", "Werkzeug", "Jinja2", "MarkupSafe", "click", "itsdangerous",
    "blinker", "prometheus-client",
)
EXPECTED_SOURCE_COMMIT = "0e2f6f3220b9fb9f0baf8730de5ffaaac00d8c38"
SOURCE_FILES = ("inventory_app.py", "http_observability.py")


def build(repo, output, expected_source_commit=EXPECTED_SOURCE_COMMIT):
    repo, output = Path(repo).resolve(), Path(output).resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", expected_source_commit):
        raise ValueError("full_expected_source_commit_required")
    top = subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], cwd=repo, timeout=5,
    ).decode().strip()
    if Path(top).resolve() != repo:
        raise ValueError("demo_repository_root_required")
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, timeout=5,
    ).decode().strip()
    if commit != expected_source_commit:
        raise ValueError("demo_source_commit_mismatch")
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *SOURCE_FILES],
        cwd=repo, timeout=5,
    )
    if dirty.strip():
        raise ValueError("demo_source_worktree_changed")
    files = {
        name: subprocess.check_output(
            ["git", "show", f"{commit}:{name}"], cwd=repo, timeout=5,
        ) for name in SOURCE_FILES
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
    output.mkdir(parents=True, exist_ok=False)
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
    parser.add_argument("--expected-source-commit", default=EXPECTED_SOURCE_COMMIT)
    args = parser.parse_args()
    build(args.repo, args.output, args.expected_source_commit)
