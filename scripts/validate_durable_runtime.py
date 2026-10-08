"""Opt-in local live validation. Own child processes only; no repair execution.

python -B scripts/validate_durable_runtime.py --inject-faults --run-model
Private output is written to ignored data/runtime-validation/.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
DEMO = Path("D:/prometheus-demo")
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import requests
from openai import OpenAI

import tools
from agent.agent import Agent
from clients.prometheus_client import PrometheusClient
from incident_store import IncidentStore
from investigation.manager import InvestigationManager
from investigation.models import HumanCommand
from investigation.store import RuntimeStore
from investigation.worker import Worker
from monitor.incident_monitor import IncidentMonitor
from prompts.agent_prompt import agent_prompt
from remediation.planner import RemediationPlanner
from remediation.store import RemediationStore


def wait_until(predicate, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except requests.RequestException:
            pass
        time.sleep(0.5)
    raise RuntimeError("live_validation_timeout")


def ensure_free(port):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))


def start_demo(service, faulty, output):
    port, module, flag = (
        (8001, "app", "SIMULATE_CODE_REGRESSION") if service == "order-service"
        else (8002, "inventory_app", "SIMULATE_INVENTORY_ERROR")
    )
    ensure_free(port)
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--", "app.py", "inventory_app.py", "http_observability.py"],
        cwd=DEMO, text=True,
    ).strip()
    if dirty:
        raise RuntimeError("demo_source_dirty_identity_unconfirmed")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=DEMO, text=True).strip()
    environment = {
        **os.environ,
        "APP_COMMIT_SHA": sha,
        flag: "true" if faulty else "false",
        "SIMULATE_SLOW_REQUEST": "false",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    child = subprocess.Popen(
        [sys.executable, "-B", "-c", f"import {module}; {module}.app.run(host='127.0.0.1',port={port},use_reloader=False)"],
        cwd=DEMO,
        env=environment,
        stdout=output,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        wait_until(lambda: requests.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200, 20)
        if child.poll() is not None:
            raise RuntimeError("owned_demo_process_not_running")
    except BaseException:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=15)
        raise
    return child


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inject-faults", action="store_true")
    parser.add_argument("--run-model", action="store_true")
    parser.add_argument("--cases", nargs="+", choices=["A", "B", "CG", "F", "E"], default=["A", "B", "CG", "F", "E"])
    args = parser.parse_args()
    if not args.inject_faults:
        parser.error("--inject-faults required: only known, initially unoccupied local Demo ports")
    ensure_free(8001)
    ensure_free(8002)
    output_dir = ROOT / "data" / "runtime-validation" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir.mkdir(parents=True)
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "scenarios": {},
        "restoration": {},
    }
    children = []
    stop_traffic = threading.Event()
    def traffic(url):
        while not stop_traffic.is_set():
            try:
                requests.get(url, timeout=5)
            except requests.RequestException:
                pass
            stop_traffic.wait(0.2)
    log = (output_dir / "demo-child.log").open("ab")
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        for service in ("order-service", "inventory-service"):
            children.append(start_demo(service, True, log))
        pool.submit(traffic, "http://127.0.0.1:8001/order")
        pool.submit(traffic, "http://127.0.0.1:8002/inventory")
        prometheus = PrometheusClient("http://localhost:9090")
        def both_alerts():
            body = prometheus.get_alerts()
            names = {a["labels"].get("service") for a in body["data"]["alerts"]
                     if a.get("state") == "firing" and a["labels"].get("alertname") == "HighHttp5xxRate"}
            return {"order-service", "inventory-service"} <= names
        wait_until(both_alerts, 90)
        print("真实 Prometheus 已产生两个 HTTP 告警。", flush=True)
        from tools.dependencies import check_dependency_health
        from tools.version import get_running_version
        from tools.logs import query_logs
        from tools.metrics import query_metrics
        from tools.code_changes import get_source_file
        version = get_running_version("order-service")
        report["tools"] = {
            "version_status": version["status"],
            "database": check_dependency_health("order-db"),
            "logs_status": query_logs("order-service")["status"],
            "metrics_status": query_metrics("order-service", "http_5xx_rate")["status"],
            "source_status": get_source_file("order-service", "app.py", version.get("commit_sha", "unknown"))["status"],
        }
        if not args.run_model:
            report["model"] = "not_requested"
            return
        client = OpenAI(api_key=os.getenv("DEEPSEEK_API_KEY"), base_url="https://api.deepseek.com", max_retries=0, timeout=60)
        def runtime(case, prompt=agent_prompt):
            directory = output_dir / case
            store = IncidentStore(directory / "incidents.json")
            manager = InvestigationManager(RuntimeStore(directory / "runtime.sqlite3"), store,
                                           RemediationPlanner(RemediationStore(directory / "remediation.json")))
            agent = Agent(client, prompt)
            monitor = IncidentMonitor(prometheus, agent, store, 30, investigation_manager=manager)
            monitor.process_alerts_once()
            return manager, agent
        def compact(manager):
            return [{
                "session_id": s.session_id,
                "service": s.service,
                "status": s.status.value,
                "model_requests": s.total_model_requests,
                "tool_executions": s.total_tool_executions,
                "cache_hits": s.cache_hits,
                "attempts": s.attempt_count,
                "resumes": s.resume_count,
                "last_error_category": s.last_error_category,
            } for s in manager.store.list_sessions()]

        if "A" in args.cases:
            manager, agent = runtime("A")
            Worker(manager, agent).run_once()
            report["scenarios"]["A_normal"] = compact(manager)
            print("场景 A: " + json.dumps(report["scenarios"]["A_normal"], ensure_ascii=False), flush=True)
            if any(s.status.value == "PAUSED" for s in manager.store.list_sessions()):
                report["model"] = "blocked_stop_further_requests"
                return

        if "B" in args.cases:
            manager, agent = runtime("B")
            began = threading.Event()
            active = manager.store.list_sessions()[0]
            def boundary(phase, session_id):
                if phase == "model_before_io":
                    began.set()
            worker = Worker(manager, agent, boundary)
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(worker.run_once)
                if not began.wait(30):
                    raise RuntimeError("model_did_not_start")
                command = manager.submit(HumanCommand(
                    session_id=active.session_id, actor="live-validation-operator", command_type="message",
                    payload={"text": "先检查数据库连接情况。"}, command_id="live-direction-message",
                ))
                future.result(timeout=300)
            report["scenarios"]["B_message"] = {
                "sessions": compact(manager),
                "command": manager.get_command(command.command_id).processing_status,
                "delivered": any(e["event_type"] == "human_message_applied" for e in manager.store.events(active.session_id)),
            }
            print("场景 B: 消息保存与交付检查完成。", flush=True)

        if "CG" in args.cases:
            manager, asking_agent = runtime("CG", "你是只读 SRE 调查 Agent。本次必须先用 request_human_input 询问：本次是否改变过数据库连接配置？未获得人类回答前不允许结束调查。收到回答后不要重复提问，继续用只读工具验证 HTTP 故障。普通回答不能批准或执行修复。")
            first = Worker(manager, asking_agent).run_once()
            requests_pending = [r for r in manager.requests(first.session_id) if r.status == "pending"]
            report["scenarios"]["C_question_before_answer"] = compact(manager)
            other = Worker(manager, Agent(client, agent_prompt)).run_once()
            report["scenarios"]["G_other_session"] = compact(manager)
            if requests_pending:
                question = requests_pending[0]
                manager.submit(HumanCommand(
                    session_id=first.session_id, actor="live-validation-operator", command_type="answer",
                    payload={"request_id": question.request_id,
                             "text": "本次受控测试仅启用了既有 Demo 故障开关，未改数据库配置。此回答仍须工具独立验证。"},
                    command_id="live-answer",
                ))
                Worker(manager, Agent(client, agent_prompt)).run_once()
            report["scenarios"]["C_answered"] = compact(manager)
            report["scenarios"]["F_approval_boundary"] = {
                "pending": [p.proposal_id for p in manager.remediation_planner.store.list("pending")],
                "approved": len(manager.remediation_planner.store.list("approved")),
                "actions": len(manager.remediation_planner.store.load()["actions"]),
            }
            print("场景 C/G/F 检查完成。", flush=True)
        if "F" in args.cases:
            manager, agent = runtime("F", "你是受控 Demo 的只读 SRE 调查员。已有 Prometheus HTTP 错误率告警。优先使用 query_logs、get_source_file（app.py，当前完整运行 SHA）和 check_dependency_health（order-db）确认应用回归路径。不要机械查询其他指标。收集证据后输出结论；不批准、不执行修复。")
            result = Worker(manager, agent).run_once()
            report["scenarios"]["F_fresh_proposal"] = {
                "sessions": compact(manager),
                "pending": [p.proposal_id for p in manager.remediation_planner.store.list("pending")],
                "approved": len(manager.remediation_planner.store.list("approved")),
                "actions": len(manager.remediation_planner.store.load()["actions"]),
            }
            print("场景 F: " + json.dumps(report["scenarios"]["F_fresh_proposal"], ensure_ascii=False), flush=True)
        if "E" in args.cases:
            from tools.code_changes import github_client
            class SlowHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    time.sleep(7)
                    self.send_response(503)
                    self.end_headers()

                def log_message(self, *args):
                    pass
            server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            previous_url = github_client.base_url
            github_started = threading.Event()
            try:
                # A genuine socket timeout through the real GithubClient, isolated
                # to this validator process; not a mock of a successful GitHub call.
                github_client.base_url = f"http://127.0.0.1:{server.server_port}"
                manager, agent = runtime("E", "你是只读 SRE 调查 Agent。先调用 get_recent_code_changes 检查变更背景；网络失败时明确证据缺口，继续使用其他只读工具验证 HTTP 故障，不将人类消息认定为根因。不审批、不执行修复。")
                original_run_tool = agent.tool_manager.run_tool
                def instrumented_tool(item):
                    if item.name == "get_recent_code_changes":
                        github_started.set()
                    return original_run_tool(item)
                agent.tool_manager.run_tool = instrumented_tool
                session = manager.store.list_sessions()[0]
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(Worker(manager, agent).run_once)
                    if not github_started.wait(45):
                        raise RuntimeError("github_fault_scenario_not_reached")
                    manager.submit(HumanCommand(
                        session_id=session.session_id, actor="live-validation-operator", command_type="message",
                        command_id="github-timeout-message", payload={"text": "先检查数据库连接情况。GitHub 超时只能视为证据缺口。"},
                    ))
                    future.result(timeout=300)
                checkpoint = manager.store.load_checkpoint(manager.store.get(session.session_id))
                report["scenarios"]["E_timeout_message"] = {
                    "sessions": compact(manager),
                    "fault_kind": "real_socket_timeout_at_controlled_loopback_endpoint",
                    "github_failures": [r["result"].get("reason") for r in checkpoint["records"] if r["name"] == "get_recent_code_changes"],
                    "delivered": any(e["event_type"] == "human_message_applied" for e in manager.store.events(session.session_id)),
                    "human_messages": len(checkpoint["messages"]),
                }
                print("场景 E: 工具传输超时与消息交付检查完成。", flush=True)
            finally:
                github_client.base_url = previous_url
                server.shutdown()
                server.server_close()
                server_thread.join()
    except Exception as exc:
        report["failure"] = {"category": type(exc).__name__}
        raise
    finally:
        stop_traffic.set()
        pool.shutdown(wait=True)
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=15)
        restored = []
        try:
            for service in ("order-service", "inventory-service"):
                restored.append(start_demo(service, False, log))
            report["restoration"] = {
                "order_status": requests.get("http://127.0.0.1:8001/order", timeout=5).status_code,
                "inventory_status": requests.get("http://127.0.0.1:8002/inventory", timeout=5).status_code,
                "order_flags": requests.get("http://127.0.0.1:8001/version", timeout=5).json()["runtime_flags"],
                "inventory_flags": requests.get("http://127.0.0.1:8002/version", timeout=5).json()["runtime_flags"],
            }
        finally:
            for child in restored:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=15)
            log.close()
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            (output_dir / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print("隔离验证记录：" + str(output_dir / "summary.json"), flush=True)


if __name__ == "__main__":
    main()
