"""Opt-in LOCAL fault integration. Writes private evidence only under ignored data/.

python -B scripts/validate_multi_signal.py --inject-faults --run-model
No remediation execution. Known Demo processes/containers only; finally restores them.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
DEMO = Path("D:/prometheus-demo").resolve()
PYTHON = Path("D:/conda/envs/agentlearn/python.exe").resolve()


def command(*args):
    return subprocess.check_output(args, cwd=DEMO, text=True, encoding="utf-8").strip()


def container_identity(name):
    expected = {
        "order-db": "postgres:17-alpine",
        "postgres-exporter": "quay.io/prometheuscommunity/postgres-exporter:v0.20.1",
    }
    # Inspect only identity fields; never return Docker environment/secrets.
    data = json.loads(command("docker", "inspect", "--format", "{{json .Id}}", name))
    image = command("docker", "inspect", "--format", "{{.Config.Image}}", name)
    working = command(
        "docker",
        "inspect",
        "--format",
        '{{index .Config.Labels "com.docker.compose.project.working_dir"}}',
        name,
    )
    if image != expected[name] or Path(working).resolve() != DEMO:
        raise RuntimeError("container_identity_unconfirmed: " + name)
    return data


def listener(port):
    script = (
        "Get-NetTCPConnection -State Listen -ErrorAction Stop | "
        f"Where-Object {{$_.LocalPort -eq {port}}} | "
        "Select-Object -ExpandProperty OwningProcess -Unique | ConvertTo-Json -Compress"
    )
    output = command("powershell", "-NoProfile", "-Command", script)
    if not output:
        return None
    value = json.loads(output)
    if isinstance(value, list):
        if len(value) != 1:
            raise RuntimeError("ambiguous_listener")
        return value[0]
    return value


def stop_service(service):
    from remediation.windows_process import ProcessHandle
    port, script = (
        (8001, "app.py") if service == "order-service" else (8002, "inventory_app.py")
    )
    pid = listener(port)
    if pid is None:
        return
    # Hold native handle during identity verification and termination (PID reuse safe).
    with ProcessHandle(pid, terminate=True) as handle:
        snapshot = handle.snapshot()
        args = snapshot["argv"]
        if (
            Path(snapshot["cwd"]).resolve() != DEMO
            or Path(snapshot["exe"]).resolve() != PYTHON
            or len(args) != 2
            or Path(args[0]).resolve() != PYTHON
            or (DEMO / args[1]).resolve() != DEMO / script
            or handle.snapshot() != snapshot
        ):
            raise RuntimeError("process_identity_unconfirmed: " + service)
        print(f"Verified stop {service} pid={pid} cwd={DEMO}", flush=True)
        if not handle.kernel.TerminateProcess(handle.handle, 0):
            raise RuntimeError("demo_stop_failed")
        if handle.kernel.WaitForSingleObject(handle.handle, 5000) != 0:
            raise RuntimeError("demo_stop_timeout")


def start_service(service, error=False, slow=False):
    import requests
    stop_service(service)
    script, port, flag = (
        ("app.py", 8001, "SIMULATE_CODE_REGRESSION")
        if service == "order-service"
        else ("inventory_app.py", 8002, "SIMULATE_INVENTORY_ERROR")
    )
    env = {
        **os.environ,
        flag: "true" if error else "false",
        "SIMULATE_SLOW_REQUEST": "true" if slow else "false",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    dirty = command(
        "git",
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        "app.py",
        "inventory_app.py",
        "http_observability.py",
    )
    env["APP_COMMIT_SHA"] = "" if dirty else command("git", "rev-parse", "HEAD")
    # APP_COMMIT_SHA is unknown for dirty sources; never misreport HEAD as deployed.
    with open(DEMO / "logs" / f"multi_signal_{service}.log", "ab") as output:
        subprocess.Popen(
            [str(PYTHON), script],
            cwd=DEMO,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    wait_until(
        lambda: requests.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code
        == 200,
        15,
        "service health",
    )


def wait_until(check, timeout, label):
    import requests
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if check():
                return
        except requests.RequestException:
            pass
        time.sleep(2)
    raise RuntimeError("timeout waiting for " + label)


def traffic(stop, url):
    import requests
    while not stop.is_set():
        try:
            requests.get(url, timeout=5)
        except requests.RequestException:
            pass
        stop.wait(0.2)


def query_signals(client):
    from services.registry import registry
    from services.metric_profiles import build_promql

    result = {}
    for service in ["order-service", "inventory-service", "order-db"]:
        profile = registry.get(service)
        for metric in (
            ["exporter_scrape_up", "postgres_up"]
            if service == "order-db"
            else [
                "service_scrape_up",
                "http_5xx_rate",
                "http_latency_p95_seconds",
                "http_request_rate",
            ]
        ):
            result[service + ":" + metric] = client.query(
                build_promql(service, profile.metrics, metric)
            )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inject-faults", action="store_true", required=True)
    parser.add_argument("--run-model", action="store_true")
    parser.add_argument(
        "--scenario",
        choices=[
            "all",
            "order-500",
            "inventory-500",
            "service-down",
            "database-down",
            "exporter-down",
            "latency",
        ],
        default="all",
    )
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    import requests
    import tools
    from agent.agent import Agent
    from clients.prometheus_client import PrometheusClient
    from incident_store import IncidentStore
    from monitor.incident_monitor import IncidentMonitor
    from models.incident import Evidence
    from prompts.agent_prompt import agent_prompt
    if Path.cwd().resolve() != ROOT or not PYTHON.is_file():
        raise RuntimeError("run_from_confirmed_agent_workspace")
    containers = {
        name: container_identity(name) for name in ["order-db", "postgres-exporter"]
    }
    mounts = json.loads(
        command("docker", "inspect", "--format", "{{json .Mounts}}", "prometheus")
    )
    expected_mount = any(
        mount.get("Destination") == "/etc/prometheus/prometheus.yml"
        and Path(mount.get("Source", "")).resolve() == DEMO / "prometheus.yml"
        for mount in mounts
    )
    if not expected_mount:
        raise RuntimeError("prometheus_identity_unconfirmed")
    output = (
        ROOT
        / "data"
        / "multi_signal_validation"
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    output.mkdir(parents=True)
    client = PrometheusClient("http://localhost:9090")
    if args.run_model:
        from openai import OpenAI

        agent = Agent(
            OpenAI(
                api_key=os.environ["DEEPSEEK_API_KEY"],
                base_url="https://api.deepseek.com",
                max_retries=0,
                timeout=60,
            ),
            agent_prompt,
        )
    else:
        agent = None
    from investigation.manager import InvestigationManager
    from investigation.store import RuntimeStore
    from investigation.worker import Worker
    store = IncidentStore(output / "incidents.json")
    manager = InvestigationManager(RuntimeStore(output / "runtime.sqlite3"), store)
    monitor = IncidentMonitor(client, store, 5, manager)
    scenarios = [
        ("order-500", "HighHttp5xxRate", "order-service"),
        ("inventory-500", "HighHttp5xxRate", "inventory-service"),
        ("service-down", "ServiceDown", "inventory-service"),
        ("database-down", "DatabaseUnavailable", "order-db"),
        ("exporter-down", "PostgresExporterDown", "order-db"),
        ("latency", "HighHttpLatency", "inventory-service"),
    ]
    stop = threading.Event()
    results = []
    pool = ThreadPoolExecutor(max_workers=4)
    try:
        start_service("order-service")
        start_service("inventory-service")
        command("docker", "restart", "prometheus")
        wait_until(
            lambda: requests.get("http://localhost:9090/-/ready", timeout=2).status_code
            == 200,
            30,
            "Prometheus ready",
        )
        for url in ["http://localhost:8001/order", "http://localhost:8002/inventory"]:
            for _ in range(2):
                pool.submit(traffic, stop, url)

        latency_threshold = next(
            float(rule["annotations"]["threshold"])
            for group in client.get_rules()["data"]["groups"]
            for rule in group["rules"]
            if rule.get("name") == "HighHttpLatency"
        )

        def healthy_baseline():
            if client.get_alerts()["data"]["alerts"]:
                return False
            signals = query_signals(client)
            for service in ["order-service", "inventory-service"]:
                expected = {
                    "service_scrape_up": lambda value: value == 1,
                    "http_5xx_rate": lambda value: value == 0,
                    "http_latency_p95_seconds": lambda value: value
                    <= latency_threshold,
                }
                for metric, check in expected.items():
                    samples = signals[service + ":" + metric]["data"]["result"]
                    if len(samples) != 1 or not check(float(samples[0]["value"][1])):
                        return False
            for metric in ["exporter_scrape_up", "postgres_up"]:
                samples = signals["order-db:" + metric]["data"]["result"]
                if len(samples) != 1 or float(samples[0]["value"][1]) != 1:
                    return False
            return True

        wait_until(
            healthy_baseline, 120, "valid healthy baseline with sufficient traffic"
        )
        for scenario, name, service in scenarios:
            if args.scenario != "all" and args.scenario != scenario:
                continue
            print(f"SCENARIO {scenario} injecting", flush=True)
            started = datetime.now(timezone.utc).isoformat()
            if scenario in ["order-500", "inventory-500"]:
                start_service(service, error=True)
            elif scenario == "latency":
                start_service(service, slow=True)
            elif scenario == "service-down":
                stop_service(service)
            else:
                target = (
                    "order-db" if scenario == "database-down" else "postgres-exporter"
                )
                if container_identity(target) != containers[target]:
                    raise RuntimeError("container_id_changed")
                command("docker", "stop", containers[target])
            wait_until(
                lambda: any(
                    a["state"] == "firing"
                    and a["labels"].get("alertname") == name
                    and a["labels"].get("service") == service
                    for a in client.get_alerts()["data"]["alerts"]
                ),
                150,
                name + " firing",
            )
            fault_snapshot = client.get_alerts()
            signals = query_signals(client)
            from alert_policy import ALERT_POLICIES
            from tools.metrics import check_scrape_target, query_metrics
            from tools.health import check_service_health

            # Explicit integration probes, separate from model-selected tool trace.
            fault_tool_probes = {
                "query_metrics": query_metrics(service, ALERT_POLICIES[name].metric),
                "check_scrape_target": check_scrape_target(service),
                "check_service_health": check_service_health(service),
            }
            (output / f"fault_{scenario}.json").write_text(
                json.dumps(
                    {
                        "alerts": fault_snapshot,
                        "signals": signals,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            active = {
                (a["labels"]["alertname"], a["labels"]["service"])
                for a in fault_snapshot["data"]["alerts"]
                if a["state"] == "firing"
            }
            allowed = {(name, service)}
            if scenario == "database-down":
                allowed.add(("HighHttp5xxRate", "order-service"))
            if not active <= allowed:
                raise RuntimeError("unrelated firing alerts: " + str(active - allowed))
            print(
                f"SCENARIO {scenario} firing={active}; real investigation starting",
                flush=True,
            )
            monitor.process_alerts_once()
            if agent is not None:
                worker = Worker(manager, agent)
                while worker.run_once() is not None:
                    pass
            incidents = [
                incident
                for incident in store.active_incidents()
                if incident.service == service and incident.alerts[-1].alertname == name
            ]
            if len(incidents) != 1:
                raise RuntimeError("expected_independent_incident_missing")
            incident = incidents[0]
            # Repeat real poll: same identity, no repeated model call within cooldown.
            before = len(incident.investigations)
            monitor.process_alerts_once()
            if (
                len(store.get_active_incident(incident.fingerprint).investigations)
                != before
            ):
                raise RuntimeError("duplicate_investigation")
            print(f"SCENARIO {scenario} restoring", flush=True)
            if scenario in ["order-500", "inventory-500", "latency", "service-down"]:
                start_service(service)
            else:
                command("docker", "start", containers[target])
            wait_until(
                lambda: not client.get_alerts()["data"]["alerts"], 120, "alerts clear"
            )
            monitor.process_alerts_once()
            business = {
                # Signal clearing can conservatively lag the first empty snapshot.
                "/order": requests.get(
                    "http://localhost:8001/order", timeout=5
                ).status_code,
                "/inventory": requests.get(
                    "http://localhost:8002/inventory", timeout=5
                ).status_code,
            }

            def incident_cleared():
                monitor.process_alerts_once()
                manager.recover()
                return store.get_active_incident(incident.fingerprint) is None

            wait_until(incident_cleared, 60, "Incident signal cleared")
            if any(code != 200 for code in business.values()):
                raise RuntimeError("business_recovery_failed")
            for row in store.load()["incidents"].values():
                if row.get("alert_state") == "cleared" and not row.get(
                    "recovery_verified_at"
                ):
                    store.record_recovery_verification(
                        row["incident_id"],
                        [
                            Evidence(
                                source="live_business_probe",
                                summary="Both Demo business routes returned 200 after clearing",
                                detail=json.dumps(business),
                            )
                        ],
                        datetime.now(timezone.utc),
                    )
            record = {
                "scenario": scenario,
                "injected_at": started,
                "incident_id": incident.incident_id,
                "model_run": args.run_model,
                "model_completed": bool(
                    args.run_model
                    and incident.investigations
                    and incident.investigations[-1].status.value == "completed"
                ),
                "fault_alerts": fault_snapshot,
                "fault_signals": signals,
                "fault_tool_probes": fault_tool_probes,
                "business_recovery": business,
                "recovery_signals": query_signals(client),
                "incident": store.load()["incidents"][incident.incident_id],
            }
            results.append(record)
            (output / "results.json").write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                f"SCENARIO {scenario} PASS incident={incident.incident_id}", flush=True
            )
    finally:
        stop.set()
        pool.shutdown(wait=True)
        # Identity-checked IDs only. Volumes are never removed.
        restore_errors = []
        for name, identity in containers.items():
            try:
                if container_identity(name) == identity:
                    command("docker", "start", identity)
                else:
                    restore_errors.append("container_identity_changed:" + name)
            except Exception as exc:
                restore_errors.append(type(exc).__name__ + ":" + name)
        for service in ["order-service", "inventory-service"]:
            try:
                start_service(service)
            except Exception as exc:
                restore_errors.append(type(exc).__name__ + ":" + service)
        if not restore_errors:
            from tools.health import check_service_health

            def restored_business():
                database = check_service_health("order-db")
                versions = {}
                business = {}
                for port, path in [(8001, "/order"), (8002, "/inventory")]:
                    business[path] = requests.get(
                        f"http://localhost:{port}{path}", timeout=5
                    ).status_code
                    versions[str(port)] = requests.get(
                        f"http://localhost:{port}/version", timeout=5
                    ).json()
                verified = (
                    database.get("query_ok") is True
                    and all(code == 200 for code in business.values())
                    and all(
                        value is False
                        for version in versions.values()
                        for value in version["runtime_flags"].values()
                    )
                )
                if verified:
                    (output / "postflight.json").write_text(
                        json.dumps(
                            {
                                "verified_at": datetime.now(timezone.utc).isoformat(),
                                "database": database,
                                "business": business,
                                "versions": versions,
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                return verified

            try:
                wait_until(
                    restored_business, 30, "restored business and disabled flags"
                )
            except Exception as exc:
                restore_errors.append(type(exc).__name__ + ":postflight")
        print(f"RESTORE errors={restore_errors}; private output={output}", flush=True)
        if restore_errors:
            raise RuntimeError("demo_restore_failed")


if __name__ == "__main__":
    main()
