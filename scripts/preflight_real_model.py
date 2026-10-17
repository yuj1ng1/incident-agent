"""P5.3c.2 isolated preflight; default mode never constructs a network client.

Paid mode is reserved for a later, explicitly approved run. No production entry
point or background service is imported. Artifacts stay in a fresh OS temp dir.
"""
import argparse
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODEL = "deepseek-flash"
BASE_URL = "https://api.deepseek.com"
MAX_MODEL_REQUESTS = 1
MAX_TOOL_EXECUTIONS = 1
MAX_OUTPUT_TOKENS = 2048
MAX_REQUEST_BYTES = 98304
MODEL_TIMEOUT_SECONDS = 30
WALL_TIMEOUT_SECONDS = 90
OFFLINE_ANSWER = "[离线传输占位回答；未调用真实模型，不证明模型理解指标。]"
TASK = """
这是固定历史案例复核，不是当前故障调查。A 为 P5.3c.1 两个历史 aligned
快照，B 为其独立指标时间错位案例；相同数值在两案出现不是独立佐证。
本次只做一次证据解释，最终回答采用以下五段，替代通用调查报告栏目：
【历史事实】【时间与可比性】【有效零、缺失与失败】
【未确认信息与根因】【后续验证】。
使用简体中文，全文约 500～800 汉字，每段用简短语句，不用表格，
不重复展开原始数据、规则或分析过程。关键事实就近引用输入中真实存在的
observation_id，不编造来源、数值或时间。区分每组评估时间和不可比较项，
说明历史证据对当前健康与恢复判断的限制，明确未经验证的信息与根因。
通用证据规则：未知不等于相同，也不等于不同。缺失时间字段不能用于证明
两个事件之间的具体时间关系。
后续验证建议最多三项，只描述验证方向，不请求或执行工具、修复或人工操作。
"""


def write_json(
    root,
    name,
    value,
):
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    write_text(root, name, text)


def redact_credential(text):
    # Preserve usage/token fields; only remove a configured credential VALUE if
    # a provider unexpectedly echoes it. Never save HTTP request headers.
    key, _ = credentials()
    if key:
        text = text.replace(key, "[credential redacted]")
    return text


def write_text(
    root,
    name,
    text,
):
    (Path(root) / name).write_text(redact_credential(text), encoding="utf-8")


def credentials():
    from dotenv import dotenv_values

    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key, "process_environment"
    key = (dotenv_values(ROOT / ".env", interpolate=False).get(
        "DEEPSEEK_API_KEY"
    ) or "").strip()
    return key, ".env" if key else "missing"


def build_case():
    from test_metric_analysis import AT, LATER, aligned, instant, record

    sources = [
        aligned("case-a-t1"),
        aligned("case-a-t2", LATER, (0.0, None, 1.0)),
        instant("case-b-errors-t1", "http_5xx_rate", 1.0, AT),
        instant("case-b-rate-t2", "http_request_rate", 0.0, LATER),
    ]
    records = [record(source) for source in sources]
    for item in records:
        item["cache_key"] = hashlib.sha256(json.dumps({
            "name": item["name"], "arguments": item["arguments"],
        }, sort_keys=True).encode("utf-8")).hexdigest()
    return {
        "schema_version": 1,
        "provenance": "P5.3c.1 A incomplete + B; imported local test fixtures",
        "not_live_prometheus": True,
        "fixture_request_executed_is_not_this_run_io": True,
        "records": records,
        "groups": {
            "A": ["case-a-t1", "case-a-t2"],
            "B": ["case-b-errors-t1", "case-b-rate-t2"],
        },
        # Kept out of the model request: this is the human review rubric.
        "expected": {
            "a": "T1 rate=0.41821223361699034, 5xx=1, scrape=1; "
                 "T2 +300ms rate=0, 5xx=no_data, scrape=1",
            "b": "independent T1 5xx=1 and T2 rate=0 are not same-time evidence",
            "zero": "finite 0.0 is an observation; no_data must not be zero-filled",
            "alignment": "A snapshots are each aligned; B has different times; "
                         "shared evaluation time is not scrape atomicity",
            "root_cause": "counter reset, database fault and code regression unverified",
            "current_health": "historical observations cannot establish current health/recovery",
            "citations": [source.observation_id for source in sources],
            "grading": "manual review required; COMPLETED is not a reasoning pass",
        },
    }


class BoundedProvider:
    """Test-only SDK boundary; no retries, second request or tool selection."""

    def __init__(
        self,
        sdk,
        root,
        live,
    ):
        self.sdk = sdk
        self.root = Path(root)
        self.live = live
        self.responses = self
        self.requests = 0
        self.real_requests = 0
        self.options = None
        self.deadline = time.monotonic() + WALL_TIMEOUT_SECONDS

    def with_options(self, *, max_retries, timeout):
        if max_retries != 0 or timeout != MODEL_TIMEOUT_SECONDS:
            raise ValueError("unsafe_sdk_options")
        self.options = {"max_retries": max_retries, "timeout": timeout}
        return self

    def create(self, **body):
        if self.requests >= MAX_MODEL_REQUESTS:
            raise ValueError("preflight_model_request_limit")
        if time.monotonic() >= self.deadline:
            raise TimeoutError("preflight_deadline")
        if self.options is None or body.get("model") != MODEL or body.get("tools") != []:
            raise ValueError("preflight_request_not_allowed")
        if set(body) != {"model", "input", "instructions", "tools"}:
            raise ValueError("preflight_request_fields_not_allowed")
        body = deepcopy(body)
        body.update(
            max_output_tokens=MAX_OUTPUT_TOKENS,
            reasoning={"effort": "none"},
            tool_choice="none",
        )
        if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("preflight_input_size_limit")
        write_json(self.root, "request.json", body)
        self.requests += 1
        write_json(self.root, "request-attempt.json", {
            "mode": "paid" if self.live else "offline",
            "sdk_entry_count": self.requests,
            "network_receipt_and_billing": "unknown" if self.live else "not_applicable",
        })
        try:
            if self.live:
                client = self.sdk.with_options(**self.options)
                self.real_requests += 1
                raw = client.responses.with_raw_response.create(**body)
                write_text(self.root, "response.raw.json", raw.text)
                response = raw.parse()
            else:
                from runtime_support import response as offline_response

                response = offline_response(answer=OFFLINE_ANSWER)
            write_json(self.root, "response.sdk.json", response.model_dump(mode="json"))
            if any(item.type not in {"message", "reasoning"} for item in response.output):
                raise ValueError("preflight_unexpected_model_tool")
            # The Runtime also persists the returned answer. Remove credential
            # echoes there without dropping usage or other raw response fields.
            serialized = response.model_dump_json()
            clean = redact_credential(serialized)
            if clean != serialized:
                response = type(response).model_validate_json(clean)
            return response
        except Exception as exc:
            write_json(self.root, "provider-error.json", {
                "category": type(exc).__name__,
                "http_status": getattr(exc, "status_code", None),
                "no_retry": True,
                "billing": "unknown" if self.live else "not_applicable",
            })
            # Worker classifies by status/type, never raw provider error text.
            # RuntimeErrorBase strings originate only from our stable codes.
            from investigation.state import RuntimeErrorBase

            if isinstance(exc, RuntimeErrorBase):
                raise RuntimeError("provider_runtime_error") from None
            raise


def isolated_tool(item, *, session_id=None):
    if item.name != "get_running_version" or json.loads(item.arguments) != {
        "service": "order-service"
    }:
        raise ValueError("preflight_tool_not_allowed")
    # There is no current process identity in a historical fixture test.
    result = {
        "status": "no_data",
        "service": "order-service",
        "reason": "historical_fixture_has_no_current_runtime_identity",
    }
    from tools.tool_manager import ToolManager

    return True, ToolManager.build_output_item(None, item, result), result


def run_case(
    root,
    *,
    live=False,
    sdk=None,
):
    root = Path(root).resolve()
    if not root.is_relative_to(Path(tempfile.gettempdir()).resolve()) or root.is_relative_to(ROOT):
        raise ValueError("preflight_requires_os_temporary_directory")
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("preflight_requires_empty_directory")
    # Setting the environment cannot redirect an already initialized logger.
    # CLI always uses a fresh child; direct embedding must not reuse a real log.
    for handler in logging.getLogger("agent").handlers:
        if not isinstance(handler, logging.FileHandler) or not Path(
            handler.baseFilename
        ).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):
            raise ValueError("preflight_requires_isolated_logger_or_fresh_process")
    os.environ["INCIDENT_AGENT_LOG_PATH"] = str(root / "agent.log")
    os.environ["OPENAI_LOG"] = "error"
    for path in (ROOT, ROOT / "tests"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    # All imports that initialize the Agent logger happen after isolation.
    from agent.agent import Agent
    from investigation.models import RuntimeLimits
    from investigation.worker import Worker
    from prompts.agent_prompt import agent_prompt
    from runtime_support import create_runtime, create_session

    key, key_source = credentials()
    if live and not key:
        raise ValueError("deepseek_api_key_missing")
    if live and sdk is None:
        from openai import OpenAI
        import httpx2

        # No proxy inheritance or redirected credential-bearing requests.
        sdk = OpenAI(
            api_key=key,
            base_url=BASE_URL,
            max_retries=0,
            timeout=MODEL_TIMEOUT_SECONDS,
            http_client=httpx2.Client(trust_env=False, follow_redirects=False),
        )
    limits = RuntimeLimits(
        max_attempts=1,
        max_model_requests=MAX_MODEL_REQUESTS,
        max_tool_executions=MAX_TOOL_EXECUTIONS,
        max_resumes=0,
        max_human_requests=0,
        model_timeout_seconds=MODEL_TIMEOUT_SECONDS,
        max_duration_seconds=WALL_TIMEOUT_SECONDS,
    )
    case = build_case()
    write_json(root, "case.json", case)
    provider = BoundedProvider(sdk, root, live)
    manager = create_runtime(root, limits=limits)
    session = create_session(manager)
    # Import raw historical inputs, not invented model tool calls/outputs or
    # executions. The event and artifact record this provenance explicitly.
    with manager.store.transaction() as db:
        saved = manager.store.get(session.session_id, db)
        checkpoint = manager.store.load_checkpoint(saved, db)
        checkpoint["records"] = deepcopy(case["records"])
        manager.store.checkpoint(saved, checkpoint, db, limits.checkpoints_to_keep)
        manager.store.save(saved, db)
        manager.store.event(db, saved.session_id, "preflight_historical_fixture_imported")
    agent = Agent(client=provider, agent_prompt=agent_prompt + TASK)
    agent.tool_manager.run_tool = isolated_tool
    agent.tool_manager.get_tool_schemas = lambda: []
    worker = Worker(manager, agent, provider_verified=False)
    # Deny all network in default mode, even accidental future tool fallbacks.
    def no_network(*args, **kwargs):
        raise AssertionError("preflight_offline_network_forbidden")

    try:
        with ExitStack() as stack:
            if not live:
                stack.enter_context(patch.object(socket.socket, "connect", no_network))
                stack.enter_context(patch.object(socket, "create_connection", no_network))
            saved = worker.run_once()
    finally:
        if live and sdk is not None:
            sdk.close()
    saved = manager.store.get(session.session_id)
    checkpoint = manager.store.load_checkpoint(saved)
    write_json(root, "checkpoint.json", checkpoint)
    write_json(root, "events.json", manager.store.events(saved.session_id, limit=1000))
    summary = {
        "mode": "paid" if live else "offline_preflight",
        "model": MODEL,
        "base_url": BASE_URL,
        "key_configured": bool(key),
        "key_source": key_source,
        "real_model_api_requests": provider.real_requests,
        "api_request_counter_semantics": "local SDK create entry, not server receipt or billing",
        "provider_request_entries": provider.requests,
        "session_status": saved.status.value,
        "error_category": saved.last_error_category,
        "runtime_model_reservations": saved.total_model_requests,
        "tool_executions": saved.total_tool_executions,
        "limits": limits.model_dump(mode="json"),
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "max_request_bytes": MAX_REQUEST_BYTES,
        "prompt_sha256": hashlib.sha256(agent_prompt.encode("utf-8")).hexdigest(),
        "packages": {name: importlib.metadata.version(name) for name in (
            "openai", "httpx2", "pydantic", "python-dotenv", "pytest"
        )},
        "manual_reasoning_review": "pending" if live else "not_run",
        "production_data_used": False,
        "prometheus_used": False,
        "remediation_planner": None,
        "artifact_directory": str(root),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(root, "summary.json", summary)
    return summary


def child_main(root, *, live=False):
    """Retain a safe startup failure when no Worker report could be written."""
    try:
        run_case(root, live=live)
    except Exception as exc:
        known_codes = {
            "deepseek_api_key_missing",
            "preflight_requires_empty_directory",
            "preflight_requires_os_temporary_directory",
            "preflight_requires_isolated_logger_or_fresh_process",
        }
        code = str(exc) if isinstance(exc, ValueError) else None
        error = {
            "category": type(exc).__name__,
            "reason": code if code in known_codes else "initialization_or_artifact_failure",
            "no_retry": True,
        }
        # The supervisor supplied a newly minted OS temp directory. Still check
        # containment before writing any diagnostic after a path rejection.
        target = Path(root).resolve()
        if (
            code != "preflight_requires_empty_directory"
            and target.is_dir()
            and target.is_relative_to(Path(tempfile.gettempdir()).resolve())
            and not target.is_relative_to(ROOT)
        ):
            (target / "startup-error.json").write_text(
                json.dumps(error), encoding="utf-8",
            )
        return 2
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true")
    parser.add_argument("--approve-paid-model-run", action="store_true")
    args = parser.parse_args()
    if args.run_live != args.approve_paid_model_run:
        parser.error("paid mode requires both flags AND prior explicit human approval")
    root = Path(tempfile.mkdtemp(prefix="incident-p53c2-"))
    # Supervise only our own child; terminate it on total deadline. Retain the
    # temp files (including request intent) after failure for manual inspection.
    code = (
        "import sys; from pathlib import Path; "
        "sys.path.insert(0,sys.argv[1]); "
        "from scripts.preflight_real_model import child_main; "
        "sys.exit(child_main(Path(sys.argv[2]),live=sys.argv[3]=='paid'))"
    )
    child = subprocess.Popen(
        [sys.executable, "-B", "-c", code, str(ROOT), str(root),
         "paid" if args.run_live else "offline"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "OPENAI_LOG": "error"},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        child.wait(timeout=WALL_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        child.terminate()
        child.wait(timeout=10)
        write_json(root, "supervisor-stop.json", {
            "reason": "wall_timeout_or_keyboard_interrupt",
            "billing": "unknown" if args.run_live else "not_applicable",
            "no_retry": True,
        })
        print(json.dumps({"stopped": True, "artifact_directory": str(root)}))
        return 2
    path = root / "summary.json"
    if child.returncode or not path.is_file():
        startup = root / "startup-error.json"
        reason = (
            json.loads(startup.read_text(encoding="utf-8"))["reason"]
            if startup.is_file() else "child_failed"
        )
        write_json(root, "supervisor-stop.json", {
            "reason": reason, "no_retry": True,
        })
        print(json.dumps({
            "failed": True, "reason": reason, "artifact_directory": str(root),
        }))
        return 2
    summary = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["session_status"] == "COMPLETED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
