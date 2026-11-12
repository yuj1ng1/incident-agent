from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from incident_factory import create_incident_from_alert
from incident_store import IncidentStore
from models.incident import Alert
from web_api.app import create_app


def save_incident(store, service, observed_at=None):
    alert = Alert(
        service=service, severity="critical", metric="http_5xx_rate",
        value=0.5, threshold=0.2, observed_at=observed_at,
    )
    incident = create_incident_from_alert(alert, service)
    store.save_incident(service, incident)
    return incident


def test_service_and_status_filters_apply_before_incident_pagination(tmp_path):
    store = IncidentStore(tmp_path / "incidents.json")
    order = save_incident(store, "order-service")
    save_incident(store, "inventory-service")
    with TestClient(create_app(incident_url=store.database_url), base_url="http://localhost") as client:
        selected = client.get("/api/incidents?limit=1&service=order-service&status=investigating")
        assert selected.status_code == 200
        assert [item["incident_id"] for item in selected.json()["items"]] == [order.incident_id]
        assert selected.json()["has_more"] is False
        assert client.get("/api/incidents?service=order-service&status=resolved").json()["items"] == []


def test_incident_signals_share_one_window_and_keep_failures_visible(tmp_path, monkeypatch):
    from tools import logs, metrics

    now = datetime.now(timezone.utc).replace(microsecond=0)
    store = IncidentStore(tmp_path / "incidents.json")
    incident = save_incident(store, "order-service", observed_at=now - timedelta(minutes=3))
    windows = []

    def metric(service, name, start, end, step):
        assert service == incident.service
        assert step == 15
        windows.append((start, end))
        if name == "http_5xx_rate":
            return {"status": "ok", "samples": [{"evaluated_at": start, "value": 0}]}
        if name == "http_latency_p95_seconds":
            return {"status": "no_data", "reason": "metrics_range_unreachable_or_invalid", "samples": []}
        return {"status": "no_data", "reason": "no_samples_in_metric_range", "samples": []}

    def log(service, start, end):
        assert service == incident.service
        windows.append((start, end))
        return {
            "status": "ok",
            "logs": [{"timestamp_utc": start, "line": "password=secret-value", "labels": {"internal": "hidden"}}],
        }

    monkeypatch.setattr(metrics, "query_metric_range", metric)
    monkeypatch.setattr(logs, "query_logs", log)
    with TestClient(create_app(incident_url=store.database_url), base_url="http://localhost") as client:
        result = client.get(f"/api/incidents/{incident.incident_id}/signals")
        missing = client.get("/api/incidents/inc-not-found/signals")
    assert result.status_code == 200
    assert missing.status_code == 404
    assert len(windows) == 5 and len(set(windows)) == 1
    payload = result.json()
    series = {entry["metric"]: entry for entry in payload["metrics"]}
    assert series["http_5xx_rate"]["samples"][0]["value"] == 0
    assert series["http_latency_p95_seconds"]["status"] == "error"
    assert series["process_cpu_cores"]["status"] == "no_data"
    assert payload["logs"]["entries"] == [{
        "timestamp": windows[0][0], "line": "password=[redacted]",
    }]
