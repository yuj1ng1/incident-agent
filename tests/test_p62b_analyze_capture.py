"""Synthetic local originals only; this suite authorizes no external activity."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import socket
import sqlite3
import subprocess

import pytest
import requests

from scripts import p62b_analyze_capture as analysis


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")


def write_manifest(root):
    write_json(root / "sha256.json", {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path != root / "sha256.json"
    })


@pytest.fixture(autouse=True)
def prohibit_external_actions(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("P6.2b capture analysis must remain local and read-only")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)


@pytest.fixture
def capture(tmp_path):
    root = tmp_path / "capture"
    traffic = root / "traffic"
    traffic.mkdir(parents=True)
    base = datetime(2026, 10, 18, 8, 45, 53, tzinfo=timezone.utc)
    write_json(root / "authorization.json", {"authorization_note": "Mock only"})
    write_json(root / "supervisor.json", {
        "mode": "p62a_owned_child_supervisor",
        "status": "deadline_exceeded",
        "started_at": base.isoformat(),
        "finished_at": (base + timedelta(seconds=60)).isoformat(),
        "elapsed_seconds_including_cleanup": 60.016,
        "business_route_requests": None,
        "business_request_count_status": "unknown",
        "originals_may_be_incomplete": True,
    })
    for number in range(1, 9):
        for offset, phase in enumerate(("identity_before", "business", "identity_after")):
            identifier = f"http-{3 * (number - 1) + offset + 1:03d}"
            began = base + timedelta(seconds=1 + (number - 1) * 7 + offset * 2)
            body = json.dumps({"order": {"private-value": "never-print-this-row"}}).encode()
            (traffic / (identifier + ".body")).write_bytes(body)
            url = analysis.PHASE_URLS[phase]
            write_json(traffic / (identifier + ".json"), {
                "id": identifier,
                "request_number": number,
                "phase": phase,
                "method": "GET",
                "url": url,
                "response_url": url,
                "query_started_at": began.isoformat(),
                "query_finished_at": (began + timedelta(seconds=2)).isoformat(),
                "elapsed_seconds": 2.0,
                "request_executed": True,
                "http_status": 200,
                "body_complete": True,
                "raw_body_file": identifier + ".body",
                "raw_body_sha256": hashlib.sha256(body).hexdigest(),
                "captured_bytes": len(body),
            })
    write_manifest(root)
    return root


def snapshot(root):
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*") if path.is_file()
    }


def change_record(root, name, **changes):
    path = root / "traffic" / name
    value = json.loads(path.read_bytes())
    value.update(changes)
    write_json(path, value)
    write_manifest(root)


def test_statistics_preserve_unknown_and_original_clock_domains(capture):
    result = analysis.analyze_capture(capture)
    assert result["fully_persisted_http_records"] == 24
    assert result["business"]["complete_records"] == 8
    assert result["business"]["http_200_records"] == 8
    assert result["business"]["elapsed_seconds"] == {
        "count": 8, "minimum": 2.0, "maximum": 2.0, "mean": 2.0, "sum": 16.0,
    }
    assert result["version"]["complete_records"] == 16
    assert result["version"]["elapsed_seconds"]["sum"] == 32.0
    assert result["business_start_intervals_seconds"] == [7.0] * 7
    assert result["business"]["utc_started_at"] == "2026-10-18T08:45:56+00:00"
    assert result["business"]["utc_finished_at"] == "2026-10-18T08:46:47+00:00"
    assert result["supervisor"]["business_route_requests"] is None
    assert result["supervisor"]["business_request_count_status"] == "unknown"
    assert result["supervisor"]["utc_wall_interval_seconds"] == 60.0
    assert result["supervisor"]["elapsed_seconds_including_cleanup"] == 60.016
    assert result["incomplete_http_records"] == []
    assert result["orphan_body_files"] == []


def test_cli_is_read_only_and_omits_body_values_and_new_timestamps(capture, capsys):
    before = snapshot(capture)
    assert analysis.main(["--capture-dir", str(capture)]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert "never-print-this-row" not in output.out
    assert "private-value" not in output.out
    result = json.loads(output.out)
    assert result["originals_modified"] is False
    assert result["row_values_included"] is False
    assert "observed_at" not in result and "analyzed_at" not in result
    assert snapshot(capture) == before


@pytest.mark.parametrize("name", ["supervisor.json", "traffic/http-002.json", "traffic/http-002.body"])
def test_manifest_tampering_is_rejected_without_rewriting(capture, name):
    path = capture / name
    path.write_bytes(path.read_bytes() + b" ")
    before = snapshot(capture)
    with pytest.raises(ValueError, match="p62b_manifest_hash_mismatch"):
        analysis.analyze_capture(capture)
    assert snapshot(capture) == before


def test_body_metadata_hash_is_checked_even_when_manifest_is_consistent(capture):
    (capture / "traffic/http-002.body").write_bytes(b"different original")
    write_manifest(capture)
    with pytest.raises(ValueError, match="p62b_body_hash_or_length_mismatch"):
        analysis.analyze_capture(capture)


@pytest.mark.parametrize("changes,error", [
    ({"query_started_at": "2026-10-18T08:45:56"}, "requires_timezone"),
    ({"query_finished_at": "2026-10-18T08:40:00Z"}, "reversed_original"),
    ({"elapsed_seconds": -1}, "invalid_original_duration"),
    ({"captured_bytes": True}, "body_hash_or_length"),
    ({"raw_body_file": "../../outside"}, "invalid_body_reference"),
])
def test_invalid_original_metadata_fails_closed(capture, changes, error):
    change_record(capture, "http-002.json", **changes)
    with pytest.raises(ValueError, match=error):
        analysis.analyze_capture(capture)


def test_timezone_is_normalized_without_renewing_record_time(capture):
    change_record(
        capture, "http-002.json",
        query_started_at="2026-10-18T16:45:56+08:00",
        query_finished_at="2026-10-18T16:45:58+08:00",
    )
    result = analysis.analyze_capture(capture)
    record = next(row for row in result["complete_records"] if row["id"] == "http-002")
    assert record["query_started_at"] == "2026-10-18T08:45:56+00:00"
    assert record["query_finished_at"] == "2026-10-18T08:45:58+00:00"


@pytest.mark.parametrize("mode", ["missing_body", "partial_body", "orphan_body"])
def test_interrupted_originals_are_reported_incomplete_not_success(capture, mode):
    if mode == "missing_body":
        (capture / "traffic/http-002.body").unlink()
        write_manifest(capture)
    elif mode == "partial_body":
        change_record(capture, "http-002.json", body_complete=False)
    else:
        (capture / "traffic/http-002.json").unlink()
        write_manifest(capture)
    result = analysis.analyze_capture(capture)
    assert result["fully_persisted_http_records"] == 23
    assert result["business"]["http_200_records"] == 7
    assert result["supervisor"]["business_request_count_status"] == "unknown"
    assert result["supervisor"]["business_route_requests"] is None
    if mode == "orphan_body":
        assert result["orphan_body_files"] == ["traffic/http-002.body"]
    else:
        assert result["incomplete_http_records"][0]["id"] == "http-002"
        assert result["incomplete_http_records"][0]["complete"] is False


def test_non_200_is_preserved_as_observation_not_business_success(capture):
    change_record(capture, "http-002.json", http_status=500)
    result = analysis.analyze_capture(capture)
    assert result["fully_persisted_http_records"] == 24
    assert result["business"]["complete_records"] == 8
    assert result["business"]["http_200_records"] == 7


def test_cli_missing_manifest_fails_without_success_output(tmp_path, capsys):
    assert analysis.main(["--capture-dir", str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {
        "status": "analysis_failed", "error": "p62b_original_read_failed",
    }


def test_unmanifested_file_is_rejected(capture):
    (capture / "traffic/http-025.body").write_bytes(b"late unverified original")
    with pytest.raises(ValueError, match="p62b_unmanifested_original"):
        analysis.analyze_capture(capture)


def test_manifest_missing_original_is_rejected(capture):
    (capture / "traffic/http-002.body").unlink()
    with pytest.raises(ValueError, match="p62b_manifest_original_missing"):
        analysis.analyze_capture(capture)
