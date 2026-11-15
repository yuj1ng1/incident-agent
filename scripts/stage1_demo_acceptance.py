"""Exercise a running Phase 1 Compose demo and restore the order-service flag."""
import argparse
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import subprocess
import time


ROOT = Path(__file__).resolve().parents[1]
ORDER_SHA = "0e2f6f3220b9fb9f0baf8730de5ffaaac00d8c38"


def get(port, path):
    connection = HTTPConnection("127.0.0.1", port, timeout=8)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        return response.status, json.loads(body) if body and body[:1] in {b"{", b"["} else None
    finally:
        connection.close()


def wait_for(description, check, *, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (OSError, ConnectionError):
            pass
        time.sleep(0.8)
    raise RuntimeError(description + "_timed_out")


def run(project, web_port, order_port):
    def compose(*arguments, capture=False, regression=False):
        return subprocess.run(
            ["docker", "compose", "--project-name", project, *arguments],
            cwd=ROOT,
            env=dict(os.environ, INCIDENT_DEMO_REGRESSION=str(regression).lower()),
            check=True, capture_output=capture, text=True, timeout=90,
        ).stdout

    def api(path):
        status, data = get(web_port, path)
        return data if status == 200 else None

    def version(regression):
        status, data = get(order_port, "/version")
        if status != 200 or data["commit_sha"] != ORDER_SHA:
            return None
        return data if data["runtime_flags"]["simulate_code_regression"] is regression else None

    def traffic(expected):
        for _ in range(6):
            status, data = get(order_port, "/order")
            if status != expected or data.get("service") != "order-service":
                raise RuntimeError(f"order_business_http_{status}_expected_{expected}")
            if expected == 500 and data.get("error_type") != "application_error":
                raise RuntimeError("unexpected_order_error_type")
            if expected == 200 and not data.get("order"):
                raise RuntimeError("healthy_order_response_missing_record")
            time.sleep(0.12)

    def recreate(regression):
        compose("up", "-d", "--no-deps", "--force-recreate", "order-service", regression=regression)
        wait_for("order_version", lambda: version(regression), timeout=40)

    container = compose("ps", "-q", "order-service", capture=True).strip()
    owned = subprocess.run(
        ["docker", "inspect", "--format", '{{index .Config.Labels "io.incident-agent.demo"}}', container],
        check=True, capture_output=True, text=True, timeout=15,
    ).stdout.strip()
    published = compose("port", "order-service", "8001", capture=True).strip()
    web_published = compose("port", "frontend", "80", capture=True).strip()
    if owned != "stage1" or published != f"127.0.0.1:{order_port}" or web_published != f"127.0.0.1:{web_port}":
        raise RuntimeError("isolated_compose_demo_required")
    if not version(False) or not api("/readyz"):
        raise RuntimeError("healthy_demo_required")

    def postgres_scraped():
        body = json.loads(compose(
            "exec", "-T", "prometheus", "wget", "-q", "-O", "-",
            "http://127.0.0.1:9090/api/v1/query?query=pg_up", capture=True,
        ))
        return any(sample["metric"].get("service") == "order-db" and sample["value"][1] == "1"
                   for sample in body["data"]["result"])

    wait_for("order_postgres_exporter", postgres_scraped, timeout=35)
    incident_list = "/api/incidents?service=order-service&limit=100"
    before = {item["incident_id"] for item in api(incident_list)["items"]}
    traffic(200)

    def fault_observation():
        traffic(500)
        for item in (api(incident_list) or {}).get("items", []):
            if item["incident_id"] in before or item["alert_state"] != "firing":
                continue
            base = "/api/incidents/" + item["incident_id"]
            incident = api(base)
            if not incident or not any(
                alert["alertname"] == "HighHttp5xxRate" and alert["state"] == "firing"
                for alert in incident["recent_alerts"]
            ):
                continue
            signals = api(base + "/signals")
            sessions = api("/api/sessions?status=COMPLETED&incident_id=" + item["incident_id"])
            if not signals or not sessions or not sessions["items"]:
                continue
            series = {entry["metric"]: entry for entry in signals["metrics"]}
            if any(entry["status"] != "ok" for entry in series.values()):
                continue
            max_5xx = max(sample["value"] for sample in series["http_5xx_rate"]["samples"])
            error_logs = [entry for entry in signals["logs"]["entries"] if "error_type=application_error" in entry["line"]]
            session = sessions["items"][0]
            if max_5xx > 0 and error_logs and session["total_tool_executions"] >= 2:
                detail = api("/api/sessions/" + session["session_id"])
                answer = (detail or {}).get("investigation_result") or {}
                if "Prometheus 查询时" not in answer.get("answer", "") or "日志包含订单应用异常" not in answer["answer"]:
                    continue
                return {
                    "incident": item["incident_id"],
                    "session": session["session_id"],
                    "alert": "HighHttp5xxRate",
                    "tool_calls": session["total_tool_executions"],
                    "max_5xx_ratio": max_5xx,
                    "metric_samples": {name: len(entry["samples"]) for name, entry in series.items()},
                    "application_error_logs": len(error_logs),
                    "window": signals["window"],
                }
        return None

    try:
        recreate(True)
        evidence = wait_for("alert_investigation_and_signals", fault_observation, timeout=150)
        print(json.dumps({"firing": evidence}), flush=True)
    finally:
        recreate(False)
        traffic(200)

    def business_recovery():
        traffic(200)
        item = api("/api/incidents/" + evidence["incident"])
        if item and item["recovery_verification"]["status"] == "verified":
            return item
        return None

    recovered = wait_for("business_recovery", business_recovery)
    session_path = "/api/sessions/" + evidence["session"]
    before_restart = api(session_path)
    compose("stop", "monitor", "api")
    compose("restart", "mysql")
    compose("up", "-d", "--wait", "--no-build", "monitor", "api")
    wait_for("restarted_api", lambda: api("/readyz"))
    session = api(session_path)
    incident = api("/api/incidents/" + evidence["incident"])
    if not session or session != before_restart or incident != recovered:
        raise RuntimeError("persisted_records_not_recovered")
    return evidence | {
        "business_recovery": incident["recovery_verification"]["status"],
        "business_http_status": incident["recovery_verification"]["last_http_status"],
        "recovery_verified_at": incident["recovery_verification"]["verified_at"],
        "restart_readback": True,
        "fault_flag_restored": bool(version(False)),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="验证隔离 Compose 的告警、调查、恢复和重启回读")
    parser.add_argument("--project", default="incident-agent-demo")
    parser.add_argument("--web-port", type=int, default=8088)
    parser.add_argument("--order-port", type=int, default=18001)
    args = parser.parse_args()
    print(json.dumps(run(args.project, args.web_port, args.order_port), ensure_ascii=False))
