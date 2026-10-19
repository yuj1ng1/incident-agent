"""P6.4 multi-round SDK fixtures over the existing P6.1 isolated Runtime.

Run synchronously in a dedicated process: transport guards and the recording
Worker subclass are scoped process-wide seams, never a second Runtime.
"""
from contextlib import ExitStack
from copy import deepcopy
from datetime import timedelta
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
from unittest.mock import patch

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import p61_readonly_integration as bootstrap


SCENARIOS = (
    "multi_round",
    "no_data",
    "tool_failure",
    "permissions",
    "saved_tool_recovery",
    "unknown_version",
    "unsupported_final",
)
MOCK_ANSWER = (
    "[P6.4 脚本化 Mock 回答，待独立审查]\n"
    "指标和日志只来自离线夹具，不能证明真实服务状态。\n"
    "服务运行版本未知时不能确认代码根因；缺失数据不能作为零或健康。\n"
    "历史记录只说明采集时的观测，尚未确认根因或业务恢复，未执行修复。"
)
UNSUPPORTED_ANSWER = (
    "[P6.4 故意包含无依据结论的 Mock 回答，必须保留原文]\n"
    "数据库已恢复，已确认代码提交导致故障。"
)


class SimulatedWorkerInterruption(BaseException):
    """Escape application exception handling after a committed observation."""


def _plan(scenario):
    metric = bootstrap._call("query_metrics", {
        "service": "order-service",
        "metric": "http_5xx_rate",
    }, "p64-metric")
    logs = bootstrap._call(
        "query_logs", {"service": "order-service"}, "p64-logs",
    )
    first = [metric]
    second = [logs]
    if scenario == "permissions":
        first += [bootstrap._call(
            "unregistered_p64_tool", {}, "p64-illegal",
        )]
        second += [
            bootstrap._call("query_metrics", {
                "service": "inventory-service",
                "metric": "http_5xx_rate",
            }, "p64-foreign"),
            bootstrap._call("execute_remediation", {
                "service": "order-service",
            }, "p64-repair"),
        ]
    answer = UNSUPPORTED_ANSWER if scenario == "unsupported_final" else MOCK_ANSWER
    responses = [
        bootstrap._response(first),
        bootstrap._response(second),
        bootstrap._response(answer=answer),
    ]
    for index, response in enumerate(responses, 1):
        response.id = f"resp-p64-mock-{index}"
        response.model = "p64-scripted-offline-fixture"
    return responses


def run_case(output_dir, scenario="multi_round"):
    """Keep every original bootstrap artifact and add independent P6.4 records."""
    if scenario not in SCENARIOS:
        raise ValueError("unknown_p64_scenario")
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=False)
    boundaries = []
    forbidden_attempts = []
    recovery = {"performed": False, "clock_advance_only_in_isolated_store": False}

    def forbidden(*args, **kwargs):
        forbidden_attempts.append("network_model_database_or_process")
        raise bootstrap.BoundaryViolation("p64_external_io_forbidden")

    with ExitStack() as stack:
        # Protect imports too. The reused bootstrap supplies its own log/HTTP
        # fixtures inside these outer transport and process boundaries.
        stack.enter_context(patch.dict(os.environ, {
            "INCIDENT_AGENT_LOG_PATH": str(root / "bootstrap.log"),
        }))
        case_logger = logging.getLogger("agent")
        stack.enter_context(patch.object(case_logger, "handlers", []))
        stack.enter_context(patch.object(case_logger, "propagate", False))
        for target in (
            "socket.socket.connect", "socket.socket.connect_ex",
            "socket.create_connection", "socket.getaddrinfo",
            "socket.socket.sendto", "requests.sessions.Session.request",
            "requests.get", "openai.OpenAI", "openai.AsyncOpenAI",
            "psycopg.connect", "subprocess.Popen", "threading.Thread.start",
        ):
            stack.enter_context(patch(target, forbidden))

        from investigation.models import Checkpoint
        from investigation.state import LostClaim
        from investigation.worker import Worker
        from tools.tool_registry import tool_registry

        class RecordingWorker(Worker):
            """Record committed state through the existing fault-injection seam."""

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.boundary_hook = self.record_boundary

            def record_boundary(self, phase, session_id):
                saved = self.store.get(session_id)
                checkpoint = self.store.load_checkpoint(saved)
                Checkpoint.model_validate(checkpoint)
                boundaries.append({
                    "boundary": phase,
                    "session": saved.model_dump(mode="json"),
                    "checkpoint": deepcopy(checkpoint),
                })
                if recovery["performed"] and "new_claim" not in recovery:
                    old = recovery["old_claim"]
                    assert saved.fencing_token > old["fencing_token"]
                    with self.store.transaction() as db:
                        try:
                            self.store.guard(
                                db, session_id, old["owner_token"],
                                old["fencing_token"],
                            )
                        except LostClaim:
                            recovery["old_claim_rejected"] = True
                        else:
                            raise AssertionError("old_claim_was_not_fenced")
                    recovery["new_claim"] = saved.model_dump(mode="json")
                if (
                    scenario == "saved_tool_recovery"
                    and not recovery["performed"]
                    and phase == "tool_checkpoint_committed"
                    and checkpoint["records"][-1]["call_id"] == "p64-metric"
                ):
                    recovery["old_claim"] = saved.model_dump(mode="json")
                    recovery["interrupted_checkpoint"] = deepcopy(checkpoint)
                    raise SimulatedWorkerInterruption()

            def run_once(self):
                try:
                    return super().run_once()
                except SimulatedWorkerInterruption:
                    recovery["performed"] = True
                    old = self.store.get(recovery["old_claim"]["session_id"])
                    assert self.store.load_checkpoint(old) == (
                        recovery["interrupted_checkpoint"]
                    )
                    # Only the injected clock changes. Manager.recover, claim,
                    # lease and fencing remain the real production operations.
                    later = old.lease_until + timedelta(seconds=1)
                    self.store.clock = lambda: later
                    recovery.update(
                        clock_advance_only_in_isolated_store=True,
                        resume_time=later.isoformat(),
                    )
                    return super().run_once()

        stack.enter_context(patch("investigation.worker.Worker", RecordingWorker))
        stack.enter_context(patch.object(
            bootstrap, "_script", lambda *args, **kwargs: _plan(scenario),
        ))
        if scenario == "tool_failure":
            def failed_logs(**arguments):
                raise RuntimeError("p64_controlled_tool_execution_failure")

            stack.enter_context(patch.dict(
                tool_registry["query_logs"], {"func": failed_logs},
            ))
        if scenario == "unknown_version":
            stack.enter_context(patch.dict(tool_registry["get_running_version"], {
                "func": lambda **arguments: {
                    "status": "no_data",
                    "service": "order-service",
                    "reason": "p64_runtime_version_unknown",
                },
            }))
        runtime = bootstrap.run_case(
            root / "runtime",
            "metrics_no_data" if scenario == "no_data" else "offline_ok",
        )
        from investigation.result_trust import project_result_trust

        trust = project_result_trust(runtime["checkpoint"].get("result"))
        assert not forbidden_attempts
        assert runtime["session"]["total_model_requests"] == 3
        assert runtime["session"]["status"] == "COMPLETED"
        assert not trust.trusted
        assert trust.semantic_status == "pending_review"
        bootstrap._write_json(root / "worker_boundaries.json", boundaries)
        report = {
            "scenario": scenario,
            "model_origin": "scripted_mock_sdk_not_real_deepseek",
            "runtime_report": "runtime/report.json",
            "boundary_snapshots": "worker_boundaries.json",
            "session": runtime["session"],
            "checkpoint": runtime["checkpoint"],
            "mock_model_requests": runtime["mock_model_requests"],
            "context_strategy": "evidence_rebuild_provider_unverified",
            "events": runtime["events"],
            "recovery": recovery,
            "result_trust": trust.model_dump(mode="json"),
            "real_model_requests": 0,
            "real_service_requests": 0,
            "business_route_requests": 0,
            "remediation_executions": 0,
            "forbidden_io_attempts": forbidden_attempts,
            "business_recovery_verified": False,
            "review_basis": "unreviewed_scripted_mock_answer",
            "human_approval": "not_recorded",
        }
        bootstrap._write_json(root / "report.json", report)
        manifest = {
            str(item.relative_to(root)): hashlib.sha256(item.read_bytes()).hexdigest()
            for item in sorted(root.rglob("*")) if item.is_file()
        }
        bootstrap._write_json(root / "sha256.json", manifest)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--scenario", choices=(*SCENARIOS, "all"), default="all")
    arguments = parser.parse_args()
    selected = SCENARIOS if arguments.scenario == "all" else [arguments.scenario]
    if arguments.scenario == "all":
        arguments.output_dir.resolve().mkdir(parents=True, exist_ok=False)
    rows = []
    for scenario in selected:
        target = (
            arguments.output_dir / scenario
            if arguments.scenario == "all" else arguments.output_dir
        )
        report = run_case(target, scenario)
        rows.append({
            "scenario": scenario,
            "report": str(target.resolve() / "report.json"),
            "session_status": report["session"]["status"],
            "mock_rounds": report["session"]["total_model_requests"],
            "tools": report["session"]["total_tool_executions"],
            "semantic_status": report["result_trust"]["semantic_status"],
            "real_model_requests": 0,
        })
    print(json.dumps(rows, ensure_ascii=False))


if __name__ == "__main__":
    main()
