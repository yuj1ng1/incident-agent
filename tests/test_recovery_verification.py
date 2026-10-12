"""P4 safety regressions: temporary manifests, mock HTTP and real subprocesses."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

from pydantic import ValidationError
import pytest
import requests

from alert_parser import AlertSnapshot
from fingerprint import generate_alert_fingerprint
from incident_factory import create_incident_from_alert
from incident_store import IncidentStore
from models.incident import AlertEvent, Evidence, Incident
from models.recovery import RecoveryReceipt
from monitor.incident_monitor import IncidentMonitor
from recovery.verifier import MAX_RESPONSE_BYTES, RecoveryVerifier, recovery_policy
from recovery.worker import RecoveryWorker
from services.configuration import SERVICES
from services.registry import ServiceRegistry, registry

ROOT = Path(__file__).resolve().parents[1]
GOOD_BODY = {"service": "order-service", "dependency": "order-db", "order": {"id": 1}}


class Clock:
    def __init__(self):
        self.now = datetime.now(timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class FakeResponse:
    def __init__(self, status=200, body=None, *, url="http://127.0.0.1:8001/order", headers=None, raw=None):
        self.status_code = status
        self.url = url
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.raw_bytes = raw if raw is not None else json.dumps(GOOD_BODY if body is None else body).encode()
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def iter_content(self, chunk_size):
        for byte in self.raw_bytes:
            yield bytes([byte])


class FakeSession:
    def __init__(self, transport):
        self.transport = transport
        self.trust_env = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url, **kwargs):
        assert self.trust_env is False
        assert kwargs["allow_redirects"] is False
        assert kwargs["stream"] is True
        assert all(value > 0 for value in kwargs["timeout"])
        return self.transport(url, **kwargs)


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    store = IncidentStore(tmp_path / "incidents.json", clock=clock)
    transport = Mock(return_value=FakeResponse())
    verifier = RecoveryVerifier(session_factory=lambda: FakeSession(transport), clock=clock)
    return SimpleNamespace(clock=clock, store=store, transport=transport, verifier=verifier,
                           worker=RecoveryWorker(store, verifier), path=tmp_path)


@pytest.fixture
def real_http_recovery(env):
    requests_seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests_seen.append(self.path)
            body = json.dumps(GOOD_BODY).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        definitions = deepcopy(SERVICES)
        definitions[0]["recovery"]["base_url"] = (
            f"http://127.0.0.1:{server.server_port}"
        )
        yield SimpleNamespace(
            verifier=RecoveryVerifier(ServiceRegistry(definitions), clock=env.clock),
            requests_seen=requests_seen,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def seed(env, *, service="order-service", fingerprint=None, cleared=True):
    alert = AlertEvent(service=service, severity="critical", metric="http_5xx_rate", value=0.5, threshold=0.2)
    fp = fingerprint or generate_alert_fingerprint(alert)
    incident = create_incident_from_alert(alert, fp)
    env.store.save_incident(fp, incident)
    if cleared:
        env.store.resolve_inactive_incidents(set(), {fp})
    return env.store.get_incident(incident.incident_id)


def seed_legacy_clear(
    env,
    version,
    marker,
):
    env.clock.advance(-86400)
    incident = seed(env)
    env.clock.advance(86400)
    data = env.store.load()
    record = data["incidents"][incident.incident_id]
    if marker == "missing":
        record.pop("recovery_verification", None)
    else:
        record["recovery_verification"] = None
    if version == 1:
        data = {incident.fingerprint: record}
    env.store.save(data)
    return reread(env, incident)


def claim(env, incident):
    return env.store.claim_recovery_verification(
        incident.incident_id, recovery_policy(env.verifier.settings(incident.service)),
    )


def save_receipt(store, receipt):
    item = Evidence(source="recovery_http", summary="caller text is ignored", detail=receipt.model_dump_json(),
                    collected_at=receipt.completed_at, observation_id=receipt.claim_token)
    return store.record_recovery_verification(receipt.incident_id, [item], receipt.completed_at,
                                               claim_token=receipt.claim_token)


def reread(env, incident):
    return env.store.get_incident(incident.incident_id, include_history=False)


def run_child(env, mode, incident, *, receipt=None):
    args = [sys.executable, "-B", str(ROOT / "tests" / "recovery_process_fixture.py"), mode,
            str(env.store.file_path), incident.incident_id, env.clock().isoformat()]
    if receipt is not None:
        path = env.path / "receipt.json"
        path.write_text(receipt.model_dump_json(), encoding="utf-8")
        args.append(str(path))
    result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    assert result.returncode == (17 if mode == "claim_exit" else 0), result.stderr
    return json.loads(result.stdout)


def test_firing_never_probes(env):
    incident = seed(env, cleared=False)
    assert env.worker.run_once() is None
    assert claim(env, incident) is None
    env.transport.assert_not_called()
    assert reread(env, incident).recovery_verified_at is None


@pytest.mark.parametrize("status,body,verdict,reason", [
    (500, GOOD_BODY, "failed", "http_status_mismatch"),
    (200, GOOD_BODY, "verified", "conditions_met"),
    (200, GOOD_BODY | {"service": "other-service"}, "failed", "business_condition_mismatch"),
    (200, GOOD_BODY | {"order": None}, "failed", "business_condition_mismatch"),
    (200, GOOD_BODY | {"order": {}}, "failed", "business_condition_mismatch"),
    (200, {"service": "order-service", "health": "ok"}, "failed", "business_condition_mismatch"),
    (302, GOOD_BODY, "insufficient", "redirect_refused"),
    (200, [], "insufficient", "invalid_response"),
])
def test_explicit_business_outcomes(env, status, body, verdict, reason):
    incident = seed(env)
    env.transport.return_value = FakeResponse(status, body)
    assert env.worker.run_once() == incident.incident_id
    saved = reread(env, incident)
    assert saved.status == "resolved" and saved.alert_state == "cleared"
    assert saved.recovery_verification.status == verdict
    assert saved.recovery_verification.reason == reason
    assert bool(saved.recovery_verified_at) is (verdict == "verified")
    assert bool(saved.recovery_evidence) is (verdict == "verified")


@pytest.mark.parametrize("error,reason", [(requests.Timeout("token=never-save"), "timeout"),
                                        (requests.ConnectionError("password=never-save"), "connection_failed")])
def test_transport_is_insufficient_and_sanitized(env, error, reason):
    incident = seed(env)
    env.transport.side_effect = error
    env.worker.run_once()
    saved = reread(env, incident)
    assert saved.recovery_verified_at is None
    assert saved.recovery_verification.reason == reason
    assert saved.recovery_verification.status == "insufficient"
    assert "never-save" not in env.store.file_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("service", ["inventory-service", "not-registered"])
def test_missing_capability_is_final_insufficient(env, service):
    incident = seed(env, service=service)
    for _ in range(5):
        env.worker.run_once()
    saved = reread(env, incident)
    assert saved.recovery_verification.reason == "recovery_capability_not_configured"
    assert saved.recovery_verification.attempts == []
    assert saved.recovery_verification.next_retry_at is None
    env.transport.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"base_url": "file:///etc/passwd"}, {"base_url": "http://user:secret@127.0.0.1:8001"},
    {"base_url": "http://@127.0.0.1:8001"}, {"base_url": "http://localhost:8001"},
    {"base_url": "http://127.0.0.1:0"}, {"base_url": "http://127.0.0.1:8001/path"},
    {"base_url": "http://169.254.169.254"}, {"base_url": "http://127.0.0.1:8001#"},
    {"endpoint": "//attacker.test/order"}, {"endpoint": "/../order"}, {"endpoint": "/%2e%2e/order"},
    {"endpoint": "/order?token=secret"}, {"method": "POST"}, {"side_effect_free": False},
    {"side_effect_free": 1}, {"side_effect_free": "true"}, {"json_equals": {"service": "other-service"}},
    {"max_attempts": 0}, {"successes_required": 6}, {"timeout_seconds": 0},
    {"evidence_max_age_seconds": 1},
])
def test_unsafe_or_unbounded_configuration_rejected(changes):
    definitions = deepcopy(SERVICES)
    definitions[0]["recovery"].update(changes)
    with pytest.raises(ValidationError):
        ServiceRegistry(definitions)


def test_missing_approval_rejected():
    definitions = deepcopy(SERVICES)
    definitions[0]["recovery"].pop("side_effect_free")
    with pytest.raises(ValidationError):
        ServiceRegistry(definitions)


def test_no_model_url_or_registered_recovery_tool(env):
    from tools.tool_registry import tool_registry
    assert "verify_recovery" not in tool_registry
    assert "recovery" in registry.capabilities("order-service")
    assert "recovery" not in registry.capabilities("inventory-service")
    assert "base_url" not in json.dumps(registry.describe("order-service"))
    incident = seed(env)
    claimed = claim(env, incident)
    with pytest.raises(TypeError):
        env.verifier.verify(claimed, url="http://attacker.test")
    env.transport.assert_not_called()


@pytest.mark.parametrize("headers,raw,reason", [
    ({"Content-Type": "text/html"}, b'<html>token=secret</html>', "invalid_response"),
    ({"Content-Encoding": "gzip"}, b'compressed', "content_encoding_refused"),
    ({"Content-Length": str(MAX_RESPONSE_BYTES + 1)}, b'', "response_too_large"),
    ({}, b'x' * (MAX_RESPONSE_BYTES + 1), "response_too_large"),
    ({}, b'{"service":"wrong","service":"order-service"}', "invalid_response"),
    ({}, b'{"service":"order-service","x":NaN}', "invalid_response"),
])
def test_invalid_bodies_and_headers_are_not_evidence(env, headers, raw, reason):
    incident = seed(env)
    response = FakeResponse(headers=headers, raw=raw)
    env.transport.return_value = response
    env.worker.run_once()
    assert reread(env, incident).recovery_verification.reason == reason
    assert response.closed
    assert "token=secret" not in env.store.file_path.read_text(encoding="utf-8")


def test_success_does_not_persist_body_or_headers(env):
    incident = seed(env)
    env.transport.return_value = FakeResponse(body=GOOD_BODY | {"token": "never-save", "password": "never-save"},
                                             headers={"Set-Cookie": "session=never-save"})
    env.worker.run_once()
    saved = IncidentStore(env.store.file_path, read_only=True).get_incident(incident.incident_id)
    receipt = RecoveryReceipt.model_validate_json(saved.recovery_evidence[0].detail)
    assert receipt.incident_id == incident.incident_id
    assert receipt.service == incident.service and receipt.target_id == "order_read_query"
    assert receipt.samples[0].http_status == 200
    assert receipt.completed_at.tzinfo is not None
    assert "never-save" not in env.store.file_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("alteration", ["missing", "naive", "old", "future", "before_clear"])
def test_invalid_time_evidence_cannot_write_success(env, alteration):
    incident = seed(env)
    if alteration == "old":
        definitions = deepcopy(SERVICES)
        definitions[0]["recovery"].update(timeout_seconds=0.1, evidence_max_age_seconds=1)
        env.verifier = RecoveryVerifier(ServiceRegistry(definitions),
                                       session_factory=lambda: FakeSession(env.transport), clock=env.clock)
    claimed = claim(env, incident)
    receipt = env.verifier.verify(claimed)
    payload = receipt.model_dump(mode="json")
    if alteration == "missing":
        del payload["samples"][0]["collected_at"]
    elif alteration == "naive":
        payload["samples"][0]["collected_at"] = env.clock().replace(tzinfo=None).isoformat()
    elif alteration == "old":
        # The lease is still live; rejection must come from evidence freshness.
        env.clock.advance(2)
    elif alteration == "future":
        payload["completed_at"] = (env.clock() + timedelta(seconds=1)).isoformat()
        payload["samples"][0]["collected_at"] = payload["completed_at"]
    else:
        payload["samples"][0]["collected_at"] = (env.clock() - timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError):
        receipt = RecoveryReceipt.model_validate(payload)
        save_receipt(env.store, receipt)
    assert reread(env, incident).recovery_verified_at is None


@pytest.mark.parametrize("field,value", [("incident_id", "wrong-id"), ("service", "inventory-service"),
                                        ("target_id", "other-target"), ("config_digest", "0" * 64),
                                        ("claim_token", "0" * 32)])
def test_identity_and_target_mismatch_rejected(env, field, value):
    incident = seed(env)
    receipt = env.verifier.verify(claim(env, incident)).model_copy(update={field: value})
    item = Evidence(source="recovery_http", summary="fixture", detail=receipt.model_dump_json(),
                    collected_at=receipt.completed_at, observation_id=receipt.claim_token)
    with pytest.raises(ValueError):
        env.store.record_recovery_verification(incident.incident_id, [item], receipt.completed_at,
                                               claim_token=receipt.claim_token)
    assert reread(env, incident).recovery_verified_at is None


def test_raw_legacy_200_cannot_attest_recovery(env):
    incident = seed(env)
    with pytest.raises(ValueError):
        env.store.record_recovery_verification(incident.incident_id,
                                               [Evidence(source="business_probe", summary="200", detail="{}")], env.clock())


def test_repeated_polls_are_idempotent(env):
    incident = seed(env)
    claimed = claim(env, incident)
    receipt = env.verifier.verify(claimed)
    assert save_receipt(env.store, receipt)
    before = env.store.file_path.read_bytes()
    assert save_receipt(env.store, receipt) is False
    for _ in range(10):
        assert env.worker.run_once() is None
    assert env.store.file_path.read_bytes() == before
    assert len(reread(env, incident).recovery_evidence) == 1
    env.transport.assert_called_once()


def test_monitor_clear_to_background_probe_pipeline(env):
    alert = AlertEvent(service="order-service", severity="critical", source="prometheus",
                       alertname="HighHttp5xxRate", signal_type="numeric", metric="http_5xx_rate",
                       value=.5, threshold=.2, unit="ratio", direction="higher_is_worse", observed_at=env.clock())
    client = SimpleNamespace(get_alerts=Mock(), get_rules=Mock(return_value={
        "status": "success", "data": {"groups": [{"rules": [{
            "name": alert.alertname, "type": "alerting", "health": "ok",
            "lastEvaluation": env.clock().isoformat(),
            "annotations": {"threshold": "0.2", "unit": "ratio", "direction": "higher_is_worse"},
        }]}]},
    }), query=Mock(side_effect=lambda expression: {
        "status": "success", "data": {"result": [{"value": [env.clock().timestamp(),
            "1" if expression.startswith("min(up{") else "0"]}]},
    }))
    snapshots = iter([AlertSnapshot(alerts=[alert], complete=True),
                      AlertSnapshot(alerts=[], complete=True), AlertSnapshot(alerts=[], complete=True)])
    monitor = IncidentMonitor(client, env.store, 1, SimpleNamespace(enqueue=Mock()),
                              normalizer=lambda _: next(snapshots), recovery_worker=env.worker)
    monitor.process_alerts_once()
    incident = env.store.active_incidents()[0]
    monitor.process_alerts_once()
    assert reread(env, incident).alert_state == "cleared"
    env.transport.assert_not_called()
    assert env.worker.wakeup.is_set()
    env.worker.run_once()
    assert reread(env, incident).recovery_verified_at is not None
    monitor.process_alerts_once()
    env.worker.run_once()
    env.transport.assert_called_once()


def test_concurrent_monitor_observation_wins_over_stale_clear(env):
    incident = seed(env, cleared=False)
    expected = reread(env, incident)
    updated = reread(env, incident)
    env.clock.advance(1)
    updated.alerts[-1].observed_at = env.clock()
    updated.alerts[-1].value = .9
    env.store.save_observation(updated.fingerprint, updated)
    assert env.store.resolve_inactive_incidents(
        set(), {incident.fingerprint}, expected_incidents={incident.fingerprint: expected},
    ) == []
    assert reread(env, incident).alert_state == "firing"
    env.worker.run_once()
    env.transport.assert_not_called()


@pytest.mark.parametrize("invalid_clear", [None, "naive", "future"])
def test_invalid_clear_time_has_no_network_probe(env, invalid_clear):
    incident = seed(env)
    data = env.store.load()
    value = None if invalid_clear is None else (
        env.clock().replace(tzinfo=None).isoformat() if invalid_clear == "naive"
        else (env.clock() + timedelta(seconds=1)).isoformat()
    )
    data["incidents"][incident.incident_id]["cleared_at"] = value
    env.store.save(data)
    env.worker.run_once()
    assert reread(env, incident).recovery_verification.reason == "clear_time_invalid"
    env.transport.assert_not_called()


def test_store_rejects_missing_checks_even_if_receipt_says_success(env):
    incident = seed(env)
    receipt = env.verifier.verify(claim(env, incident))
    receipt.samples[0].checks = receipt.samples[0].checks[:-1]
    with pytest.raises(ValueError, match="recovery_success_conditions_unconfirmed"):
        save_receipt(env.store, receipt)
    assert reread(env, incident).recovery_verified_at is None


def test_boolean_business_condition_does_not_accept_integer(env):
    definitions = deepcopy(SERVICES)
    definitions[0]["recovery"]["json_equals"]["ready"] = True
    verifier = RecoveryVerifier(ServiceRegistry(definitions), session_factory=lambda: FakeSession(env.transport), clock=env.clock)
    incident = seed(env)
    env.transport.return_value = FakeResponse(body=GOOD_BODY | {"ready": 1})
    RecoveryWorker(env.store, verifier).run_once()
    assert reread(env, incident).recovery_verification.reason == "business_condition_mismatch"


def test_failed_verification_keeps_historical_evidence(env):
    incident = seed(env)
    data = env.store.load()
    historical = Evidence(source="historical", summary="historical", detail="{}").model_dump(mode="json")
    data["incidents"][incident.incident_id]["recovery_evidence"].append(historical)
    env.store.save(data)
    env.transport.return_value = FakeResponse(500)
    env.worker.run_once()
    saved = reread(env, incident)
    assert saved.recovery_verified_at is None
    assert saved.recovery_evidence[0].source == "historical"


def test_failed_retries_are_durable_delayed_and_bounded(env):
    incident = seed(env)
    env.transport.return_value = FakeResponse(500)
    for attempt in range(3):
        worker = RecoveryWorker(IncidentStore(env.store.file_path, clock=env.clock), env.verifier)
        assert worker.run_once() == incident.incident_id
        assert len(reread(env, incident).recovery_verification.attempts) == attempt + 1
        for _ in range(5):
            assert worker.run_once() is None
        env.clock.advance(30)
    assert reread(env, incident).recovery_verification.next_retry_at is None
    for _ in range(5):
        env.clock.advance(60)
        env.worker.run_once()
    assert env.transport.call_count == 3


def test_restart_discovers_cleared_incident_without_queued_memory(env):
    incident = seed(env)
    # Durable jobs survive downtime; age alone cannot decide eligibility.
    env.clock.advance(86400)
    restarted = RecoveryWorker(IncidentStore(env.store.file_path, clock=env.clock), env.verifier)
    restarted.run_once()
    saved = reread(env, incident)
    receipt = RecoveryReceipt.model_validate_json(saved.recovery_evidence[0].detail)
    assert saved.cleared_at == receipt.cleared_at == incident.cleared_at
    assert saved.recovery_verified_at == receipt.completed_at == env.clock()
    assert saved.recovery_verified_at - saved.cleared_at == timedelta(days=1)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("marker", ["missing", "null"])
def test_first_worker_skips_yesterdays_legacy_clear_without_real_http(
    env,
    real_http_recovery,
    version,
    marker,
):
    incident = seed_legacy_clear(env, version, marker)
    before = env.store.file_path.read_bytes()
    restarted_store = IncidentStore(env.store.file_path, clock=env.clock)
    restarted = RecoveryWorker(restarted_store, real_http_recovery.verifier)

    processed = restarted.run_once()
    assert real_http_recovery.requests_seen == []
    assert processed is None
    assert restarted_store.recovery_candidates() == []
    assert env.clock() - incident.cleared_at == timedelta(days=1)
    for _ in range(5):
        assert restarted.run_once() is None
    saved = reread(env, incident)
    assert saved.recovery_verified_at is None
    assert saved.recovery_evidence == []
    assert saved.recovery_verification is None
    assert env.store.file_path.read_bytes() == before


def test_background_startup_only_probes_durable_jobs(env, real_http_recovery):
    historical = seed_legacy_clear(env, 2, "missing")
    current = seed(env, fingerprint="new-episode")
    restarted_store = IncidentStore(env.store.file_path, clock=env.clock)
    restarted = RecoveryWorker(
        restarted_store,
        real_http_recovery.verifier,
        interval=0.01,
    )
    restarted.start()
    try:
        deadline = time.monotonic() + 3
        # Share the Worker's read/write lock while its threads are committing.
        while True:
            saved = restarted_store.get_incident(
                current.incident_id,
                include_history=False,
            )
            if saved.recovery_verified_at is not None:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        restarted.stop()
    assert real_http_recovery.requests_seen == ["/order"]
    assert reread(env, historical).recovery_verified_at is None
    assert reread(env, historical).recovery_verification is None


@pytest.mark.parametrize("marker", ["missing", "null"])
def test_direct_claim_does_not_enroll_unregistered_clear(env, marker):
    incident = seed_legacy_clear(env, 2, marker)
    before = env.store.file_path.read_bytes()
    for _ in range(5):
        assert claim(env, incident) is None
    assert env.store.file_path.read_bytes() == before
    env.transport.assert_not_called()


@pytest.mark.parametrize("previously_verified", [False, True])
def test_legacy_clear_preserves_existing_history(env, previously_verified):
    incident = seed_legacy_clear(env, 2, "missing")
    data = env.store.load()
    record = data["incidents"][incident.incident_id]
    record["recovery_evidence"] = [
        Evidence(
            source="historical",
            summary="legacy observation",
            detail="{}",
            collected_at=incident.cleared_at,
        ).model_dump(mode="json"),
    ]
    if previously_verified:
        record["recovery_verified_at"] = incident.cleared_at.isoformat()
    env.store.save(data)
    before = env.store.file_path.read_bytes()
    for _ in range(5):
        assert env.worker.run_once() is None
        assert claim(env, incident) is None
    saved = reread(env, incident)
    assert saved.recovery_verification is None
    assert saved.recovery_evidence[0].source == "historical"
    assert saved.recovery_verified_at == (
        incident.cleared_at if previously_verified else None
    )
    assert env.store.file_path.read_bytes() == before
    env.transport.assert_not_called()


def test_normal_clear_commits_pending_task_before_first_worker(env):
    incident = seed(env, cleared=False)
    assert env.store.resolve_inactive_incidents(set(), {incident.fingerprint}) == [
        incident.incident_id,
    ]
    saved = reread(env, incident)
    state = saved.recovery_verification
    assert state.status == "pending"
    assert state.cleared_at == saved.cleared_at == env.clock()
    assert state.policy is None and state.attempts == []
    assert saved.recovery_verified_at is None and saved.recovery_evidence == []
    assert env.store.recovery_candidates() == [incident.incident_id]
    before = env.store.file_path.read_bytes()
    assert env.store.resolve_inactive_incidents(set()) == []
    assert env.store.file_path.read_bytes() == before
    env.transport.assert_not_called()


def test_pending_task_and_clear_are_committed_atomically(env, monkeypatch):
    incident = seed(env, cleared=False)
    before = env.store.file_path.read_bytes()

    def fail_commit(data):
        raise OSError("isolated_commit_failure")

    with monkeypatch.context() as patch:
        patch.setattr(env.store, "save", fail_commit)
        with pytest.raises(OSError, match="isolated_commit_failure"):
            env.store.resolve_inactive_incidents(set())
    assert env.store.file_path.read_bytes() == before
    assert reread(env, incident).alert_state == "firing"
    assert reread(env, incident).recovery_verification is None
    assert env.store.recovery_candidates() == []


def test_skipped_history_does_not_block_new_pending_task(env):
    historical = seed_legacy_clear(env, 2, "missing")
    current = seed(env, fingerprint="new-episode")
    assert env.store.recovery_candidates() == [current.incident_id]
    assert env.worker.run_once() == current.incident_id
    assert reread(env, historical).recovery_verified_at is None
    assert reread(env, historical).recovery_verification is None
    assert reread(env, current).recovery_verified_at is not None
    env.transport.assert_called_once()


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_firing_record_can_enqueue_when_cleared_after_upgrade(env, version):
    incident = seed(env, cleared=False)
    data = env.store.load()
    record = data["incidents"][incident.incident_id]
    record.pop("recovery_verification")
    if version == 1:
        data = {incident.fingerprint: record}
    env.store.save(data)
    assert env.store.resolve_inactive_incidents(set()) == [incident.incident_id]
    restarted = RecoveryWorker(
        IncidentStore(env.store.file_path, clock=env.clock),
        env.verifier,
    )
    assert restarted.run_once() == incident.incident_id
    saved = reread(env, incident)
    assert saved.recovery_verification.status == "verified"
    assert saved.recovery_verified_at is not None
    env.transport.assert_called_once()


def test_abrupt_process_exit_recovers_after_lease(env):
    incident = seed(env)
    assert run_child(env, "claim_exit", incident)["token"]
    attempt = reread(env, incident).recovery_verification.attempts[-1]
    assert env.worker.run_once() is None
    env.clock.now = attempt.lease_until + timedelta(seconds=1)
    restarted = RecoveryWorker(IncidentStore(env.store.file_path, clock=env.clock), env.verifier)
    restarted.run_once()
    saved = reread(env, incident)
    assert saved.recovery_verification.attempts[0].outcome == "abandoned"
    assert len(saved.recovery_verification.attempts) == 2
    assert saved.recovery_verified_at is not None


@pytest.mark.parametrize("same_id,clear_again", [(True, False), (True, True), (False, False), (False, True)])
def test_refiring_during_probe_rejects_old_success(env, same_id, clear_again):
    incident = seed(env)

    def refire(*args, **kwargs):
        env.clock.advance(1)
        if same_id:
            latest = reread(env, incident)
            latest.status, latest.alert_state = "investigating", "firing"
            env.store.save_observation(latest.fingerprint, latest)
        else:
            seed(env, fingerprint=incident.fingerprint, cleared=False)
        if clear_again:
            env.store.resolve_inactive_incidents(set())
        return FakeResponse()

    env.transport.side_effect = refire
    env.worker.run_once()
    saved = reread(env, incident)
    assert saved.recovery_verified_at is None
    assert saved.recovery_verification.status == "superseded"
    assert saved.recovery_evidence == []


def test_configuration_change_does_not_reset_budget_or_allow_old_writer(env):
    incident = seed(env)
    old = env.verifier.verify(claim(env, incident))
    definitions = deepcopy(SERVICES)
    definitions[0]["recovery"]["successes_required"] = 2
    changed = RecoveryVerifier(ServiceRegistry(definitions), session_factory=lambda: FakeSession(env.transport), clock=env.clock)
    RecoveryWorker(env.store, changed).run_once()
    assert reread(env, incident).recovery_verification.reason == "configuration_changed"
    with pytest.raises(ValueError):
        save_receipt(env.store, old)
    assert len(reread(env, incident).recovery_verification.attempts) == 1


def test_stale_generic_save_preserves_claim_and_receipt(env):
    incident = seed(env)
    stale = reread(env, incident)
    env.worker.run_once()
    env.store.save_incident(incident.fingerprint, stale)
    saved = reread(env, incident)
    assert saved.recovery_verified_at is not None
    assert len(saved.recovery_evidence) == 1
    assert len(saved.recovery_verification.attempts) == 1


def test_cross_process_expired_owner_cannot_write_success(env):
    incident = seed(env)
    old = env.verifier.verify(claim(env, incident))
    env.clock.advance(15)
    new_token = run_child(env, "claim", incident)["token"]
    assert new_token and new_token != old.claim_token
    assert run_child(env, "record", incident, receipt=old)["accepted"] is False
    new = env.verifier.verify(reread(env, incident))
    assert run_child(env, "record", incident, receipt=new)["accepted"] is True
    saved = reread(env, incident)
    assert len(saved.recovery_evidence) == 1
    assert saved.recovery_evidence[0].observation_id == new_token


def test_two_processes_can_only_claim_once(env):
    incident = seed(env)
    args = [sys.executable, "-B", str(ROOT / "tests" / "recovery_process_fixture.py"), "claim",
            str(env.store.file_path), incident.incident_id, env.clock().isoformat()]
    processes = [subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    outputs = [process.communicate(timeout=15) for process in processes]
    assert all(process.returncode == 0 for process in processes), outputs
    tokens = [json.loads(stdout)["token"] for stdout, _ in outputs]
    assert sum(token is not None for token in tokens) == 1
    assert len(reread(env, incident).recovery_verification.attempts) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows open-reader replacement semantics")
def test_cross_process_reader_failure_is_bounded_and_resumes_safely(env):
    incident = seed(env)
    ready, release = env.path / "reader-ready", env.path / "reader-release"
    args = [sys.executable, "-B", str(ROOT / "tests" / "recovery_process_fixture.py"), "hold_read",
            str(env.store.file_path), incident.incident_id, env.clock().isoformat(), str(ready), str(release)]
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        started = time.monotonic()
        with pytest.raises(PermissionError):
            env.worker.run_once()
        assert time.monotonic() - started < 1
        assert reread(env, incident).recovery_verified_at is None
        env.transport.assert_not_called()
    finally:
        release.touch()
        stdout, stderr = process.communicate(timeout=15)
    assert process.returncode == 0, stderr
    # The blocked reader sees the pending task committed with the clear.
    assert json.loads(stdout)["old_snapshot_has_recovery"] is True
    RecoveryWorker(IncidentStore(env.store.file_path, clock=env.clock), env.verifier).run_once()
    assert reread(env, incident).recovery_verified_at is not None


@pytest.mark.parametrize("legacy", [1, 2])
def test_historical_data_remains_readable(env, legacy):
    incident = seed(env)
    record = incident.model_dump(mode="json")
    for key in ("recovery_verification", "recovery_evidence", "recovery_verified_at", "alert_state", "cleared_at"):
        record.pop(key)
    data = {incident.fingerprint: record} if legacy == 1 else {
        "version": 2, "incidents": {incident.incident_id: record}, "active_by_fingerprint": {},
    }
    env.store.file_path.write_text(json.dumps(data), encoding="utf-8")
    saved = IncidentStore(env.store.file_path, read_only=True).get_incident(incident.incident_id)
    assert saved.alert_state == "cleared"
    assert saved.recovery_verified_at is None and saved.recovery_verification is None
    assert saved.recovery_evidence == []
    before = env.store.file_path.read_bytes()
    for _ in range(5):
        assert env.worker.run_once() is None
        assert claim(env, saved) is None
    assert env.store.file_path.read_bytes() == before
    env.transport.assert_not_called()


def test_probe_runs_outside_file_and_sqlite_locks(env):
    from investigation.store import RuntimeStore
    runtime = RuntimeStore(env.path / "runtime.sqlite3")
    incident = seed(env)

    def prove_lock_free(*args, **kwargs):
        # Another store instance and a real SQLite writer can operate during HTTP.
        other = IncidentStore(env.store.file_path, clock=env.clock)
        data = other.load()
        other.save(data)
        with runtime.transaction() as db:
            runtime.metadata(db, "probe_fixture", True, write=True)
        return FakeResponse()

    env.transport.side_effect = prove_lock_free
    env.worker.run_once()
    assert reread(env, incident).recovery_verified_at is not None


def test_slow_recovery_does_not_block_monitor_or_other_recovery(env):
    first = seed(env)
    definitions = deepcopy(SERVICES)
    second_profile = deepcopy(definitions[0])
    second_profile["name"] = "second-service"
    second_profile["recovery"]["base_url"] = "http://127.0.0.1:8002"
    second_profile["recovery"]["json_equals"]["service"] = "second-service"
    definitions.append(second_profile)
    second = seed(env, service="second-service")
    entered, release, second_done = threading.Event(), threading.Event(), threading.Event()

    def transport(url, **kwargs):
        if ":8001" in url:
            entered.set()
            assert release.wait(5)
            return FakeResponse(500)
        second_done.set()
        return FakeResponse(body=GOOD_BODY | {"service": "second-service"}, url=url)

    verifier = RecoveryVerifier(ServiceRegistry(definitions), session_factory=lambda: FakeSession(transport), clock=env.clock)
    worker = RecoveryWorker(env.store, verifier, interval=0.01)
    worker.start()
    try:
        assert entered.wait(3) and second_done.wait(3)
        manager = SimpleNamespace(enqueue=Mock())
        alert = AlertEvent(service="inventory-service", severity="critical", metric="http_5xx_rate", value=.5, threshold=.2)
        monitor = IncidentMonitor(Mock(), env.store, 1, manager,
                                  normalizer=lambda _: AlertSnapshot(alerts=[alert], complete=False), recovery_worker=worker)
        started = time.monotonic()
        monitor.process_alerts_once()
        assert time.monotonic() - started < 1
        manager.enqueue.assert_called_once()
    finally:
        release.set()
        worker.stop()
    assert reread(env, first).recovery_verified_at is None
    assert reread(env, second).recovery_verified_at is not None


def test_success_count_is_configured_and_partial_results_fail(env):
    definitions = deepcopy(SERVICES)
    definitions[0]["recovery"]["successes_required"] = 2
    verifier = RecoveryVerifier(ServiceRegistry(definitions), session_factory=lambda: FakeSession(env.transport), clock=env.clock)
    incident = seed(env)
    env.transport.side_effect = [FakeResponse(), FakeResponse(500)]
    RecoveryWorker(env.store, verifier).run_once()
    saved = reread(env, incident)
    assert saved.recovery_verified_at is None
    assert len(saved.recovery_verification.attempts[-1].receipt.samples) == 2
    env.clock.advance(30)
    env.transport.side_effect = [FakeResponse(), FakeResponse()]
    RecoveryWorker(env.store, verifier).run_once()
    assert reread(env, incident).recovery_verified_at is not None
    assert env.transport.call_count == 4


def test_real_http_transport_against_isolated_loopback_server(env, real_http_recovery):
    incident = seed(env)
    RecoveryWorker(env.store, real_http_recovery.verifier).run_once()
    assert reread(env, incident).recovery_verified_at is not None
    assert real_http_recovery.requests_seen == ["/order"]


def test_real_redirect_is_refused_before_library_drains_body(env):
    release = threading.Event()
    requests_seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests_seen.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/must-not-follow")
            self.send_header("Content-Length", "2000000")
            self.end_headers()
            # There is deliberately no body to drain. Reject on headers alone.
            release.wait(5)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        definitions = deepcopy(SERVICES)
        definitions[0]["recovery"].update(base_url=f"http://127.0.0.1:{server.server_port}", timeout_seconds=0.2)
        verifier = RecoveryVerifier(ServiceRegistry(definitions), clock=env.clock)
        incident = seed(env)
        RecoveryWorker(env.store, verifier).run_once()
        saved = reread(env, incident)
        assert saved.recovery_verified_at is None
        assert saved.recovery_verification.reason == "redirect_refused"
        assert saved.recovery_verification.attempts[-1].receipt.samples[0].http_status == 302
        assert requests_seen == ["/order"]
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()
