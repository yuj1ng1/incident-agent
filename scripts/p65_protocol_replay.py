"""Replay a sealed first Provider response offline; no paid execution option."""
import argparse
import json
from pathlib import Path

from scripts import p65_readonly_investigation as p65
from scripts.p65_model_boundary import ROOT, encoded, seal, sha, write

DEFAULT_SOURCE = ROOT / "data/p65-live-20261019-01"


def read_frozen(source):
    """Verify every sealed file before parsing; never change the source set."""
    source = Path(source).resolve(strict=True)
    manifest = json.loads((source / "sha256.json").read_bytes())
    if {p.name for p in source.iterdir()} != set(manifest) | {"sha256.json"}:
        raise ValueError("p65_frozen_file_set_changed")
    for name, digest in manifest.items():
        path = source / name
        if Path(name).name != name or path.is_symlink() or sha(path.read_bytes()) != digest:
            raise ValueError("p65_frozen_integrity_failed")
    wire = (source / "response-01.raw.txt").read_bytes()
    integrity = json.loads((source / "response-01.integrity.json").read_bytes())
    if integrity["credential_redacted"] or integrity["wire_text_sha256"] != sha(wire):
        raise ValueError("p65_frozen_wire_not_original")
    return wire, json.loads((source / "response-01.sdk.json").read_bytes())


def run_case(source, output):
    """Use the existing isolated Manager/Worker path, then a labelled Mock final."""
    source = Path(source).resolve(strict=True)
    first_response = read_frozen(source)
    original_request = json.loads((source / "request-01.json").read_bytes())
    original_summary = json.loads((source / "summary.json").read_bytes())
    approval = json.loads((source / "authorization.json").read_bytes())
    # Read only: this record is evidence of consumption, never permission here.
    consumed = ROOT / "data/p65-paid-consumption" / (
        approval["plan_sha256"] + ".consumed.json"
    )
    consumption = json.loads(consumed.read_bytes())
    if consumption["approval"] != approval:
        raise ValueError("p65_frozen_consumption_binding_mismatch")
    capture = p65.DEFAULT_CAPTURE
    package = p65.evidence_package(capture)
    body = p65.first_request(package)
    if (body != original_request["body"]
            or sha(encoded(body)) != original_request["sha256"]):
        raise ValueError("p65_replay_request_scope_changed")
    root = p65._separate(output, source, consumed.parent)
    root.mkdir(parents=True, exist_ok=False)
    p65.prepare(capture, root / "prepared-offline-only")
    followup = p65.mock_plan("no_tools")
    followup[0].id = "p65-scripted-mock-followup-final"
    summary = p65.run_prepared(
        root / "prepared-offline-only",
        root / "runtime",
        offline_first_response=first_response,
        mock_responses=followup,
    )
    audit = {
        "mode": "real_provider_first_response_offline_replay",
        "followup": "scripted_mock_followup",
        "source_dir": str(source),
        "source_manifest_sha256": sha((source / "sha256.json").read_bytes()),
        "raw_sha256": sha(first_response[0]),
        "sdk_sha256": sha((source / "response-01.sdk.json").read_bytes()),
        "consumed_authorization_sha256": sha(consumed.read_bytes()),
        "original_session": original_summary["session"],
        "runtime_summary": "runtime/summary.json",
        "session": summary.get("session"),
        "response_origins": summary.get("response_origins"),
        "result_trust": summary.get("result_trust"),
        "real_model_sdk_requests": summary["real_model_sdk_requests"],
        "forbidden_io_attempts": summary["forbidden_io_attempts"],
        "real_multiround_investigation_passed": False,
    }
    # Recheck after the Worker run, including the original failed Session bytes.
    if read_frozen(source) != first_response:
        raise ValueError("p65_frozen_changed_during_replay")
    write(root, "replay-audit.json", audit)
    seal(root)
    return audit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    audit = run_case(args.source_dir, args.output_dir)
    print(json.dumps({
        "mode": audit["mode"],
        "session_status": (audit["session"] or {}).get("status"),
        "real_model_sdk_requests": audit["real_model_sdk_requests"],
    }, ensure_ascii=False))
    return 0 if (audit["session"] or {}).get("status") == "COMPLETED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
