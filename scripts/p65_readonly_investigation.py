"""Prepare P6.5 offline; --execute requires NEW, separately recorded approval.

Run synchronously in a dedicated process. Temporary process-wide guards and
tool function replacements follow the P6.1/P6.4 isolation convention.
"""
from contextlib import ExitStack
from copy import deepcopy
import argparse
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import sys
from unittest.mock import patch

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.p65_model_boundary import (
    BASE_URL, BoundedResponses, MAX_MODELS, MAX_OUTPUT_TOKENS, MAX_TOOLS,
    MODEL, ROOT, TASK, TIMEOUT, TOOLS, encoded, inspect_input, seal, sha,
    utc, validate_approval, write,
)
from scripts.p63_evidence import DEFAULT_CAPTURE, build_context
from scripts.p61_readonly_integration import BoundaryViolation, _call, _response

CODE_FILES = (
    "scripts/p65_readonly_investigation.py", "scripts/p65_model_boundary.py",
    "scripts/p63_evidence.py", "scripts/p63_model_validation.py",
    "scripts/p62b_evidence.py", "scripts/p62b_error_experiment.py",
    "agent/agent.py", "model_client/model_client.py", "tools/tool_manager.py",
    "tools/tool_registry.py", "investigation/worker.py", "investigation/store.py",
    "investigation/manager.py", "investigation/models.py",
    "investigation/checkpoint.py", "investigation/evidence_policy.py",
    "investigation/result_trust.py", "config.py",
)


def evidence_package(capture):
    """Reuse P6.3 allowlisted projection and P6.1 non-current replay envelope."""
    context = build_context(capture)
    from scripts.p62b_evidence import verify_capture

    originals = verify_capture(Path(capture) / "observations")["observations"]
    selected = {}
    for key, row in originals.items():
        if row["tool"] not in TOOLS:
            continue
        projected = next(s for s in context["evidence_index"]["sources"]
                         if s["name"] == row["tool"] and (
                             row["tool"] != "query_metrics"
                             or s["value"]["metric"] == row["arguments"]["metric"]
                         ))
        selected[key] = {
            "arguments": row["arguments"],
            "result": {
                "status": "no_data", "service": "order-service",
                "reason": "historical_replay_cannot_answer_current_state",
                "historical_only": True, "use_for_current_state": False,
                "evidence_scope": "time_window",
                "captured_at": row["collected_at"],
                "source": row["source"], "originals": projected["originals"],
                "captured_result": deepcopy(row["result"]),
                "sanitized_projection": projected,
                "fault_window": context["fault_window"],
                "collection_window": context["collection_window"],
            },
        }
    package = {"context": context, "observations": selected}
    inspect_input(package)
    return package


def schemas():
    from tools.tool_registry import tool_registry

    result = []
    for name in TOOLS:
        schema = deepcopy(tool_registry[name]["schema"])
        schema["description"] = (
            "仅回放已批准的 order-service 冻结历史材料；不能回答当前状态。"
            "省略日志时间表示固定采集窗口；其他时间拒绝。 "
            + schema["description"]
        )
        schema["parameters"]["properties"]["service"]["enum"] = ["order-service"]
        schema["parameters"]["additionalProperties"] = False
        result.append(schema)
    return result


def first_request(package):
    context = package["context"]
    return {
        "model": MODEL,
        "instructions": TASK,
        "input": [{"role": "user", "content": encoded({
            "task": "调查冻结历史现象，按需要自主调用只读工具。",
            "service": "order-service",
            "alert_input_origin": "synthetic_alert_input",
            "real_firing_observed": False,
            "historical_only": True, "use_for_current_state": False,
            "capture_manifest_sha256": context["capture_manifest_sha256"],
            "fault_window": context["fault_window"],
            "collection_window": context["collection_window"],
            "known_gaps": context["known_gaps"],
            "automatic_version_probe": package["observations"]["get_running_version"]["result"],
            "max_model_requests": MAX_MODELS,
            "max_tool_executions_including_probe": MAX_TOOLS,
        }).decode("utf-8")}],
        "tools": schemas(), "tool_choice": "auto",
        "reasoning": {"effort": "none"},
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }


def _separate(output, *protected):
    path = Path(output).resolve()
    for other in (*protected, DEFAULT_CAPTURE, ROOT / "data/p63-live-20261018-01",
                  ROOT / "data/p63-live-review-20261018-01"):
        source = Path(other).resolve()
        if path.is_relative_to(source) or source.is_relative_to(path):
            raise ValueError("p65_output_overlaps_protected_input")
    return path


def prepare(capture, output):
    package = evidence_package(capture)
    body = first_request(package)
    plan = {
        "phase": "p65_offline_prepared_pending_new_approval",
        "model": MODEL, "base_url": BASE_URL,
        "capture_dir": str(Path(capture).resolve()),
        "capture_manifest_sha256": package["context"]["capture_manifest_sha256"],
        "package_sha256": sha(encoded(package)),
        "first_request_sha256": inspect_input(body),
        "code_sha256": {name: sha((ROOT / name).read_bytes()) for name in CODE_FILES},
        "max_model_requests": MAX_MODELS, "max_tool_executions": MAX_TOOLS,
        "max_attempts": 1, "max_incidents": 1,
        "sdk_retries": 0, "application_retries": 0,
        "timeout_seconds": TIMEOUT, "max_output_tokens": MAX_OUTPUT_TOKENS,
        "tools": list(TOOLS), "real_model_sdk_requests": 0,
        "historical_only": True, "use_for_current_state": False,
        "provider_verified": False,
        "context_strategy": "actual_committed_outputs_phase_local_protocol_probe",
        "sdk_versions": {k: importlib.metadata.version(k)
                         for k in ("openai", "httpx2", "pydantic")},
    }
    root = _separate(output, capture)
    root.mkdir(parents=True, exist_ok=False)
    write(root, "package.json", package)
    write(root, "first-request.json", body)
    write(root, "plan.json", plan)
    seal(root)
    return plan


def read_prepared(prepared):
    root = Path(prepared).resolve(strict=True)
    expected = {"package.json", "first-request.json", "plan.json"}
    if {p.name for p in root.iterdir()} != expected | {"sha256.json"}:
        raise ValueError("p65_prepared_file_set_changed")
    manifest = json.loads((root / "sha256.json").read_bytes())
    if set(manifest) != expected or any(
        (root / name).is_symlink() or sha((root / name).read_bytes()) != digest
        for name, digest in manifest.items()
    ):
        raise ValueError("p65_prepared_integrity_failed")
    plan = json.loads((root / "plan.json").read_bytes())
    package = evidence_package(plan["capture_dir"])
    body = first_request(package)
    if (encoded(package) != (root / "package.json").read_bytes()
            or encoded(body) != (root / "first-request.json").read_bytes()
            or sha(encoded(package)) != plan["package_sha256"]
            or inspect_input(body) != plan["first_request_sha256"]
            or plan["code_sha256"] != {
                name: sha((ROOT / name).read_bytes()) for name in CODE_FILES
            }
            or plan["max_model_requests"] != MAX_MODELS
            or plan["max_tool_executions"] != MAX_TOOLS):
        raise ValueError("p65_prepared_scope_or_code_changed")
    return plan, package, body


def mock_plan(scenario="multi_round"):
    """Fixtures are explicitly scripted, never real autonomous tool selection."""
    calls = [
        _response([_call("query_metrics", {
            "service": "order-service", "metric": "http_5xx_rate",
        }, "p65-metric")]),
        _response([_call("query_logs", {"service": "order-service"}, "p65-logs")]),
        _response(answer="[P6.5 离线 Mock] 只有冻结历史观测；根因与当前业务恢复待验证。"),
    ]
    if scenario == "no_tools":
        calls = calls[-1:]
    for index, response in enumerate(calls, 1):
        response.id = f"p65-scripted-{index}"
        response.model = MODEL
    return calls


def run_prepared(
    prepared,
    output,
    *,
    live=False,
    authorization=None,
    mock_responses=None,
    sdk_factory=None,
    consumption_dir=None,
    boundary_hook=None,
    offline_first_response=None,
):
    if live and offline_first_response is not None:
        raise ValueError("p65_replay_cannot_enter_sdk")
    # Missing approval is rejected before package reads, credentials or SDK.
    if live and authorization is None:
        raise ValueError("p65_new_explicit_authorization_required")
    plan, package, body = read_prepared(prepared)
    if live:
        validate_approval(authorization, plan)
    if consumption_dir is not None and sdk_factory is None:
        raise ValueError("p65_custom_ledger_only_for_injected_test_sdk")
    root = _separate(output, prepared, plan["capture_dir"])
    root.mkdir(parents=True, exist_ok=False)
    boundaries = []
    forbidden_attempts = []
    sdk = None
    adapter = None
    session_id = None
    manager = None
    summary = {
        "mode": (
            "real_provider_first_response_offline_replay"
            if offline_first_response is not None else
            "paid" if live else "scripted_mock_sdk_not_real_deepseek"
        ),
        "status": "failed", "started_at": utc(),
        "real_model_sdk_requests": 0, "sdk_retries": 0,
        "application_retries": 0, "real_service_requests": 0,
        "business_route_requests": 0, "remediation_executions": 0,
        "provider_verified": False, "business_recovery_verified": False,
        "alert_input_origin": "synthetic_alert_input",
        "real_firing_observed": False,
        "historical_only": True, "use_for_current_state": False,
        "plan_sha256": sha(encoded(plan)),
        "context_strategy": plan["context_strategy"],
        "human_approval": "not_recorded",
        "counter_semantics": "local_sdk_create_entry_not_server_receipt_or_billing",
    }

    def forbidden(*args, **kwargs):
        forbidden_attempts.append("external_io_or_remediation")
        raise BoundaryViolation("p65_external_io_forbidden")

    try:
        key = ""
        if live:
            # One plan gets one reservation, regardless of copied package/path.
            ledger = Path(consumption_dir or ROOT / "data/p65-paid-consumption")
            ledger.mkdir(parents=True, exist_ok=True)
            write(ledger, sha(encoded(plan)) + ".consumed.json", {
                "reserved_at": utc(), "approval": authorization,
                "outcome": "reserved_never_automatically_replayed",
            })
            write(root, "authorization.json", authorization)
            from scripts.preflight_real_model import credentials

            key, key_source = credentials()
            if not key:
                raise ValueError("p65_deepseek_credential_missing")
            import httpx2
            from openai import OpenAI

            def endpoint_gate(request):
                if str(request.url) != BASE_URL + "/responses":
                    raise ValueError("p65_provider_endpoint_changed")

            sdk = (sdk_factory or OpenAI)(
                api_key=key, base_url=BASE_URL, max_retries=0, timeout=TIMEOUT,
                http_client=httpx2.Client(
                    trust_env=False, follow_redirects=False,
                    event_hooks={"request": [endpoint_gate]},
                ),
            )
            summary["credential_source"] = key_source
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {
                "INCIDENT_AGENT_LOG_PATH": str(root / "agent.log"),
            }))
            logger = logging.getLogger("agent")
            handler = logging.FileHandler(root / "agent.log", mode="x", encoding="utf-8")
            stack.callback(handler.close)
            stack.enter_context(patch.object(logger, "handlers", [handler]))
            stack.enter_context(patch.object(logger, "propagate", False))
            for target in (
                "requests.get", "requests.sessions.Session.request", "psycopg.connect",
                "subprocess.Popen", "threading.Thread.start",
                "remediation.executor.ActionExecutor.execute",
                "recovery.worker.RecoveryWorker.start",
            ):
                stack.enter_context(patch(target, forbidden))
            if not live:
                for target in (
                    "socket.socket.connect", "socket.socket.connect_ex",
                    "socket.create_connection", "socket.getaddrinfo",
                    "socket.socket.sendto", "openai.OpenAI", "openai.AsyncOpenAI",
                ):
                    stack.enter_context(patch(target, forbidden))
            else:
                # Network opens only while the fixed-endpoint SDK operation is
                # entered. Tool/Runtime I/O stays blocked even in paid mode.
                import socket

                for obj, name in (
                    (socket.socket, "connect"), (socket.socket, "connect_ex"),
                    (socket, "create_connection"), (socket, "getaddrinfo"),
                ):
                    original = getattr(obj, name)

                    def guarded(*args, _original=original, **kwargs):
                        if adapter is None or not adapter.in_sdk:
                            return forbidden()
                        return _original(*args, **kwargs)

                    stack.enter_context(patch.object(obj, name, guarded))
                stack.enter_context(patch("socket.socket.sendto", forbidden))
            from agent.agent import Agent
            from fingerprint import generate_alert_fingerprint
            from incident_factory import create_incident_from_alert
            from incident_store import IncidentStore
            from investigation.manager import InvestigationManager
            from investigation.models import Checkpoint, RuntimeLimits
            from investigation.result_trust import project_result_trust
            from investigation.store import RuntimeStore
            from investigation.worker import Worker
            from models.incident import Alert
            from tools.tool_registry import tool_registry

            def replay(name):
                def read(**arguments):
                    if arguments.get("service") != "order-service":
                        return {"status": "no_data", "reason": "p65_target_denied"}
                    row = next((r for r in package["observations"].values()
                                if r["arguments"] == arguments and (
                                    name != "query_metrics"
                                    or r["result"]["captured_result"].get("metric")
                                    == arguments.get("metric")
                                ) and r["result"]["source"] == {
                                    "query_metrics": "http://localhost:9090/api/v1/query",
                                    "query_logs": "http://localhost:3100/loki/api/v1/query_range",
                                    "get_running_version": "http://localhost:8001/version",
                                }[name]), None)
                    if name == "query_logs" and arguments == {"service": "order-service"}:
                        row = package["observations"]["query_logs"]
                    return deepcopy(row["result"]) if row else {
                        "status": "no_data", "reason": "outside_approved_frozen_evidence",
                        "historical_only": True, "use_for_current_state": False,
                        "evidence_scope": "time_window",
                    }
                return read

            # Scoped reduction of the existing registry, never a second registry.
            allowed = {s["name"]: {"schema": s, "func": replay(s["name"])}
                       for s in body["tools"]}
            stack.enter_context(patch.dict(tool_registry, allowed, clear=True))
            limits = RuntimeLimits(
                max_attempts=1, max_model_requests=MAX_MODELS,
                max_tool_executions=MAX_TOOLS, max_resumes=0,
                max_human_requests=0, model_timeout_seconds=TIMEOUT,
            )
            manager = InvestigationManager(
                RuntimeStore(root / "runtime.sqlite3"),
                IncidentStore(root / "incidents.json"), limits=limits,
            )
            # Synthetic event only bootstraps Runtime; it asserts no live firing.
            alert = Alert(
                service="order-service", severity="critical", metric="http_5xx_rate",
                value=0.27, threshold=0.2, labels={"origin": "synthetic_alert_input"},
            )
            fingerprint = generate_alert_fingerprint(alert)
            incident = create_incident_from_alert(alert, fingerprint)
            manager.incident_store.save_incident(fingerprint, incident)
            session = manager.enqueue(incident)
            session_id = session.session_id

            def snapshot():
                saved = manager.store.get(session_id)
                return saved, manager.store.load_checkpoint(saved)

            adapter = BoundedResponses(
                root, body, snapshot, sdk=sdk, credential=key,
                mock_responses=mock_responses if mock_responses is not None else mock_plan(),
                offline_first_response=offline_first_response,
            )
            agent = Agent(adapter, TASK)

            def boundary(phase, identity):
                saved, checkpoint = snapshot()
                Checkpoint.model_validate(checkpoint)
                boundaries.append({
                    "phase": phase, "session": saved.model_dump(mode="json"),
                    "checkpoint": checkpoint,
                })
                if boundary_hook is not None:
                    boundary_hook(phase, manager, saved, checkpoint)

            worker = Worker(manager, agent, boundary_hook=boundary)
            # No scheduler loop, recovery thread, retry or automatic second run.
            worker.run_once()
            saved, checkpoint = snapshot()
            summary.update(
                status="finished", session=saved.model_dump(mode="json"),
                checkpoint=checkpoint,
                incident=manager.incident_store.get_incident(
                    saved.incident_id,
                ).model_dump(mode="json"),
                events=manager.store.events(session_id, limit=1000),
                result_trust=project_result_trust(checkpoint.get("result")).model_dump(mode="json"),
            )
            chosen = [i for p in root.glob("response-*.sdk.json")
                      for i in json.loads(p.read_bytes())["output"]
                      if i["type"] == "function_call"]
            summary["model_selected_calls"] = chosen
            summary["response_origins"] = [
                json.loads(p.read_bytes())["response_origin"]
                for p in sorted(root.glob("completion-*.json"))
            ]
            summary["outcome"] = (
                (
                    "real_model_completed_without_tool_use" if live
                    else "mock_completed_without_tool_use"
                )
                if saved.status.value == "COMPLETED" and not chosen
                else "completed_pending_review" if saved.status.value == "COMPLETED"
                else "stopped_incomplete_or_failed"
            )
            result = checkpoint.get("result") or {}
            if saved.status.value == "COMPLETED":
                with (root / "answer-original.txt").open("xb") as handle:
                    handle.write(result["answer"].encode("utf-8"))
    except Exception as exc:
        summary["error"] = {"category": type(exc).__name__}
        if isinstance(exc, ValueError) and str(exc).startswith("p65_"):
            summary["error"]["code"] = str(exc)
    finally:
        if sdk is not None:
            sdk.close()
        if manager is not None and session_id is not None and "session" not in summary:
            saved = manager.store.get(session_id)
            summary["session"] = saved.model_dump(mode="json")
            summary["checkpoint"] = manager.store.load_checkpoint(saved)
        write(root, "worker-boundaries.json", boundaries)
        summary.update(
            finished_at=utc(), forbidden_io_attempts=forbidden_attempts,
            real_model_sdk_requests=adapter.sdk_entries if live and adapter else 0,
            adapter_requests=adapter.requests if adapter else 0,
        )
        write(root, "summary.json", summary)
        seal(root)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-dir", type=Path, default=DEFAULT_CAPTURE)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--prepared-dir", type=Path)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--authorization", type=Path)
    args = parser.parse_args(argv)
    if args.mock and args.execute or (args.mock or args.execute) and args.prepared_dir is None:
        parser.error("mock/execute require --prepared-dir and are mutually exclusive")
    if args.authorization and not args.execute:
        parser.error("authorization requires --execute")
    if args.mock or args.execute:
        result = run_prepared(
            args.prepared_dir, args.output_dir, live=args.execute,
            authorization=(
                json.loads(args.authorization.read_bytes())
                if args.authorization else None
            ),
        )
    else:
        result = prepare(args.capture_dir, args.output_dir)
    print(json.dumps({k: result[k] for k in (
        "status", "phase", "real_model_sdk_requests", "outcome", "first_request_sha256",
    ) if k in result}, ensure_ascii=False))
    failed = (
        result.get("status") == "failed"
        or result.get("outcome") == "stopped_incomplete_or_failed"
    )
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
