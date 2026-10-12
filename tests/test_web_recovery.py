"""Recovery views over isolated persisted records; HTTP reads never run probes."""
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from incident_store import IncidentStore
from models.incident import Evidence, Incident
from models.recovery import (
    RecoveryAttempt, RecoveryCheck, RecoveryCondition, RecoveryPolicy,
    RecoveryReceipt, RecoverySample, RecoveryVerification,
)
from web_api.app import create_app


CLEARED_AT = datetime(2026, 10, 12, 6, 36, 39, tzinfo=timezone.utc)


def recovery_record(
    status="verified",
    *,
    attempt_count=1,
    max_attempts=3,
    check_count=3,
):
    incident = Incident(
        incident_id="inc-web-recovery",
        fingerprint="fixture-fingerprint",
        service="fixture-service",
        severity="warning",
        status="resolved",
        alert_state="cleared",
        cleared_at=CLEARED_AT,
        alerts=[],
    )
    checks = [
        RecoveryCondition(kind="http_status"),
        RecoveryCondition(kind="json_equals", field="service"),
    ] + [
        RecoveryCondition(kind="nonempty_object", field=f"business_{number}")
        for number in range(check_count - 2)
    ]
    policy = RecoveryPolicy(
        target_id="fixture_query",
        config_digest="a" * 64,
        expected_status=200,
        required_checks=checks,
        successes_required=1,
        max_attempts=max_attempts,
        retry_seconds=30,
        evidence_max_age_seconds=60,
        lease_seconds=20,
    )
    attempts = []
    for number in range(0 if status == "pending" else attempt_count):
        last = number == attempt_count - 1
        outcome = {
            "running": "running", "verified": "success", "failed": "failed",
            "insufficient": "insufficient", "superseded": "insufficient",
        }[status] if last else "failed"
        reason = {
            "running": None, "success": "conditions_met",
            "failed": "business_condition_mismatch", "insufficient": "invalid_response",
        }[outcome]
        started_at = CLEARED_AT + timedelta(seconds=number * 10 + 1)
        attempt = RecoveryAttempt(
            token=f"{number + 1:032x}",
            started_at=started_at,
            lease_until=started_at + timedelta(seconds=20),
            completed_at=None if outcome == "running" else started_at + timedelta(seconds=2),
            outcome=outcome,
            reason=reason,
        )
        if outcome != "running":
            receipt = RecoveryReceipt(
                incident_id=incident.incident_id,
                service=incident.service,
                target_id=policy.target_id,
                config_digest=policy.config_digest,
                claim_token=attempt.token,
                cleared_at=CLEARED_AT,
                started_at=started_at,
                completed_at=started_at + timedelta(seconds=1),
                outcome=outcome,
                reason=reason,
                samples=[RecoverySample(
                    collected_at=started_at + timedelta(seconds=1),
                    http_status=200,
                    checks=[RecoveryCheck(
                        **check.model_dump(),
                        matched=outcome == "success" or index < len(checks) - 1,
                    ) for index, check in enumerate(checks)],
                    outcome=outcome,
                    reason=reason,
                )],
            )
            if outcome == "success":
                incident.recovery_verified_at = receipt.completed_at
                incident.recovery_evidence = [Evidence(
                    source="recovery_http",
                    summary="fixture recovery observation",
                    detail=receipt.model_dump_json(),
                    observation_id=attempt.token,
                    collected_at=receipt.completed_at,
                )]
            else:
                attempt.receipt = receipt
        attempts.append(attempt)
    incident.recovery_verification = RecoveryVerification(
        cleared_at=CLEARED_AT,
        status=status,
        policy=policy,
        attempts=attempts,
        reason=attempts[-1].reason if attempts else None,
        next_retry_at=attempts[-1].completed_at + timedelta(seconds=30)
        if status in {"failed", "insufficient"} and len(attempts) < max_attempts
        else None,
    )
    return incident.model_dump(mode="json")


@pytest.fixture
def recovery_api(tmp_path):
    path = tmp_path / "incidents.json"
    app = create_app(incident_path=path, runtime_path=tmp_path / "unused.sqlite3")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        def write(record):
            path.write_text(json.dumps({
                "version": 2,
                "incidents": {record["incident_id"]: record},
                "active_by_fingerprint": {},
            }), encoding="utf-8")
        yield path, client, write


def read_view(client):
    result = client.get("/api/incidents/inc-web-recovery")
    assert result.status_code == 200
    return result.json()["recovery_verification"]


@pytest.mark.parametrize("status", [
    "pending", "running", "verified", "failed", "insufficient", "superseded",
])
def test_persisted_recovery_status_and_attempts_are_mapped(recovery_api, status):
    _, client, write = recovery_api
    record = recovery_record(status)
    write(record)
    view = read_view(client)
    assert view["status"] == status
    assert view["target_id"] == "fixture_query"
    assert view["attempt_count"] == (0 if status == "pending" else 1)
    assert view["max_attempts"] == 3
    assert view["next_retry_at"] == record["recovery_verification"]["next_retry_at"]
    assert bool(view["verified_at"]) is (status == "verified")
    if view["attempts"]:
        latest = view["attempts"][-1]
        assert view["last_attempt_at"] == latest["started_at"]
        assert view["last_http_status"] == latest["http_status"]
        assert len(latest["checks"]) == (0 if status == "running" else 3)
        if status == "verified":
            assert all(check["matched"] for check in latest["checks"])
        elif status != "running":
            assert any(not check["matched"] for check in latest["checks"])


def test_recovery_view_is_an_explicit_recursive_whitelist(recovery_api):
    _, client, write = recovery_api
    record = recovery_record()
    record["recovery_evidence"][0]["summary"] = "private-response-body"
    record["recovery_evidence"][0]["response_headers"] = {"Authorization": "private-header"}
    record["internal_debug"] = {"url": "https://private.example/order"}
    write(record)
    result = client.get("/api/incidents/inc-web-recovery")
    assert result.status_code == 200
    view = result.json()["recovery_verification"]
    assert set(view) == {
        "status", "target_id", "attempt_count", "max_attempts", "last_attempt_at",
        "next_retry_at", "reason", "last_http_status", "verified_at", "attempts",
    }
    assert set(view["attempts"][0]) == {
        "number", "started_at", "completed_at", "outcome", "reason", "http_status", "checks",
    }
    assert set(view["attempts"][0]["checks"][0]) == {"kind", "field", "matched"}
    for forbidden in (
        "claim_token", "lease_until", "receipt", "config_digest", "policy",
        "raw_response", "response_headers", "private-response-body", "private-header",
        "private.example", "internal_debug", "a" * 64, f"{1:032x}",
    ):
        assert forbidden not in result.text
    assert "recovery_verification" not in client.get("/api/incidents").json()["items"][0]


@pytest.mark.parametrize("legacy_timestamp", [False, True])
@pytest.mark.parametrize("v1", [False, True])
def test_historical_clears_do_not_imply_business_verification(recovery_api, legacy_timestamp, v1):
    path, client, write = recovery_api
    record = recovery_record()
    record.pop("recovery_verification")
    record.pop("alert_state")
    record["recovery_evidence"] = []
    if not legacy_timestamp:
        record.pop("recovery_verified_at")
    write(record)
    if v1:
        path.write_text(json.dumps({record["fingerprint"]: record}), encoding="utf-8")
    before = path.read_bytes()
    view = read_view(client)
    assert view["status"] == ("legacy_only" if legacy_timestamp else "not_recorded")
    assert view["verified_at"] is None
    assert view["attempts"] == [] and view["attempt_count"] == 0
    assert view["max_attempts"] is None and view["reason"] is None
    assert path.read_bytes() == before


@pytest.mark.parametrize("with_task", [False, True])
def test_firing_alert_takes_precedence_over_resolution_and_old_success(recovery_api, with_task):
    _, client, write = recovery_api
    record = recovery_record()
    record["alert_state"] = "firing"
    if not with_task:
        record["recovery_verification"] = None
    write(record)
    view = read_view(client)
    assert view["status"] == "alert_firing" and view["verified_at"] is None


@pytest.mark.parametrize("corruption", [
    "missing", "json", "oversize", "incident", "service", "target", "token",
    "digest", "clear_time", "timestamp", "incomplete_checks", "extra_debug",
])
def test_unusable_success_receipts_are_insufficient_and_never_leak(recovery_api, corruption, caplog):
    _, client, write = recovery_api
    record = recovery_record()
    evidence = record["recovery_evidence"][0]
    receipt = json.loads(evidence["detail"])
    if corruption == "missing":
        record["recovery_evidence"] = []
    elif corruption == "json":
        evidence["detail"] = '{"password":"hidden-receipt-secret"'
    elif corruption == "oversize":
        evidence["detail"] = "hidden-receipt-secret" * 2000
    else:
        key, value = {
            "incident": ("incident_id", "inc-other"),
            "service": ("service", "other-service"),
            "target": ("target_id", "other-target"),
            "token": ("claim_token", "f" * 32),
            "digest": ("config_digest", "f" * 64),
            "clear_time": ("cleared_at", "2026-10-12T06:36:38Z"),
            "timestamp": ("completed_at", "2026-10-12T06:36:43Z"),
            "incomplete_checks": ("samples", [{
                **receipt["samples"][0], "checks": receipt["samples"][0]["checks"][:1],
            }]),
            "extra_debug": ("response_body", "hidden-receipt-secret"),
        }[corruption]
        receipt[key] = value
        evidence["detail"] = json.dumps(receipt)
    write(record)
    view = read_view(client)
    assert view["status"] == "insufficient" and view["reason"] == "invalid_evidence"
    assert view["verified_at"] is None and view["last_http_status"] is None
    assert view["attempts"][0]["outcome"] == "insufficient"
    assert view["attempts"][0]["checks"] == []
    assert "hidden-receipt-secret" not in json.dumps(view) + caplog.text


def test_attempt_and_check_limits_and_retry_exhaustion(recovery_api):
    _, client, write = recovery_api
    write(recovery_record("failed", attempt_count=5, max_attempts=5, check_count=21))
    view = read_view(client)
    assert view["attempt_count"] == view["max_attempts"] == len(view["attempts"]) == 5
    assert all(len(attempt["checks"]) == 21 for attempt in view["attempts"])
    assert view["next_retry_at"] is None
    assert len(json.dumps(view).encode("utf-8")) < 20000


@pytest.mark.parametrize("overflow", ["attempts", "checks", "target", "reason"])
def test_invalid_counts_identifiers_and_uncontrolled_reasons_fail_safely(recovery_api, overflow, caplog):
    _, client, write = recovery_api
    record = recovery_record("failed")
    state = record["recovery_verification"]
    if overflow == "attempts":
        state["attempts"] *= 6
    elif overflow == "checks":
        state["attempts"][0]["receipt"]["samples"][0]["checks"] *= 8
    elif overflow == "target":
        state["policy"]["target_id"] = "https://hidden-target.example/?token=hidden-input-secret"
    else:
        state["reason"] = "hidden-input-secret"
    write(record)
    result = client.get("/api/incidents/inc-web-recovery")
    assert result.status_code == 500 and result.json() == {"detail": "store_read_failed"}
    assert "hidden-input-secret" not in result.text + caplog.text
    assert "hidden-target.example" not in result.text + caplog.text


def test_changed_clear_batch_does_not_display_old_verified_result(recovery_api):
    _, client, write = recovery_api
    record = recovery_record()
    record["cleared_at"] = (CLEARED_AT + timedelta(minutes=1)).isoformat()
    write(record)
    view = read_view(client)
    assert view["status"] == "superseded" and view["reason"] == "alert_changed"
    assert view["verified_at"] is None


def test_superseded_task_keeps_its_status_when_old_receipt_is_unusable(recovery_api):
    _, client, write = recovery_api
    record = recovery_record("superseded")
    state = record["recovery_verification"]
    state["reason"] = "alert_changed"
    state["attempts"][0]["receipt"]["target_id"] = "other-target"
    write(record)
    view = read_view(client)
    assert view["status"] == "superseded" and view["reason"] == "alert_changed"
    assert view["verified_at"] is None
    assert view["attempts"][0]["checks"] == []


def test_registered_identifiers_are_masked_without_exposing_secret_values(recovery_api, monkeypatch):
    _, client, write = recovery_api
    secret = "ghp_12345678901234567890"
    monkeypatch.setenv("RECOVERY_TEST_API_KEY", secret)
    record = recovery_record("failed")
    state = record["recovery_verification"]
    state["policy"]["target_id"] = secret
    receipt = state["attempts"][0]["receipt"]
    receipt["target_id"] = secret
    state["policy"]["required_checks"][2]["field"] = secret
    receipt["samples"][0]["checks"][2]["field"] = secret
    write(record)
    view = read_view(client)
    assert secret not in json.dumps(view)
    assert "redacted" in view["target_id"]


@pytest.mark.parametrize("status", ["pending", "running", "failed", "verified"])
def test_detail_reads_cannot_claim_probe_retry_or_write(recovery_api, status):
    path, client, write = recovery_api
    write(recovery_record(status))
    before = path.read_bytes()
    files_before = set(path.parent.rglob("*"))
    blocked = AssertionError("GET invoked recovery work")
    with (
        patch.object(IncidentStore, "claim_recovery_verification", side_effect=blocked),
        patch.object(IncidentStore, "record_recovery_verification", side_effect=blocked),
        patch.object(IncidentStore, "finish_recovery_verification", side_effect=blocked),
        patch.object(IncidentStore, "save", side_effect=blocked),
        patch("recovery.verifier.RecoveryVerifier.verify", side_effect=blocked),
        patch("recovery.worker.RecoveryWorker.__init__", side_effect=blocked),
        patch("requests.Session.request", side_effect=blocked),
        patch("openai.OpenAI", side_effect=blocked),
    ):
        assert read_view(client)["status"] == status
        assert read_view(client)["status"] == status
        for method in ("post", "put", "patch", "delete"):
            assert getattr(client, method)("/api/incidents/inc-web-recovery").status_code == 405
    assert path.read_bytes() == before
    assert set(path.parent.rglob("*")) == files_before
