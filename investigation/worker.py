"""Single synchronous Worker: durable I/O boundaries and cooperative interruption."""
from datetime import datetime, timedelta
import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import openai
from pydantic import ValidationError
from agent_logger.logger_config import logger

from config import MAX_AGENT_LOOPS, MAX_TOOL_CALLS
from investigation.checkpoint import protect, restore_context, valid_chain
from investigation.evidence_policy import TTL, cache_key, effective_records, fresh
from investigation.metric_analysis import (
    MAX_ANALYSIS_INPUT_BYTES,
    MAX_ANALYSIS_OBSERVATIONS,
    MAX_ANALYSIS_OUTPUT_BYTES,
    MAX_ANALYSIS_SAMPLES,
    analyze_metric_evidence,
)
from investigation.models import HumanInputArguments, SessionStatus as S
from investigation.state import AttemptToolBudgetExhausted, LostClaim, RuntimeErrorBase, RuntimeIntegrityError, budget_exhaustion, exhausted, transition
from models.incident import Evidence
from models.investigation import InvestigationResult, InvestigationStatus
from services.registry import registry
from tools.tool_registry import tool_registry

METRIC_ANALYSIS_TOOLS = frozenset({
    "query_metrics",
    "query_metric_range",
    "query_metrics_aligned",
})
MAX_METRIC_ANALYSIS_CONTEXT_BYTES = MAX_ANALYSIS_OUTPUT_BYTES

METRIC_RANGE_CONTEXT_FIELDS = (
    "status",
    "reason",
    "service",
    "metric",
    "evidence_scope",
    "use_for_current_state",
    "requested_range",
    "query_range",
    "step_seconds",
    "point_limit",
    "expected_point_count",
    "sample_count",
    "missing_point_count",
    "truncated",
    "query_started_at",
    "query_finished_at",
)

METRICS_ALIGNED_CONTEXT_FIELDS = (
    "status",
    "reason",
    "service",
    "evidence_scope",
    "use_for_current_state",
    "requested_metrics",
    "requested_evaluation_time",
    "evaluation_time",
    "timestamp_resolution",
    "evaluation_time_adjustment_ns",
    "comparison_complete",
    "results",
    "consistency_warnings",
    "limitations",
    "query_started_at",
    "query_finished_at",
)

HUMAN_TOOL = {
    "type": "function",
    "name": "request_human_input",
    "description": "请求人类提供现有只读工具无法获得的必要背景；暂停调查等待回答，不具备审批权限。",
    "parameters": {
        "type": "object",
        "properties": {"question": {"type": "string", "minLength": 1, "maxLength": 2000}},
        "required": ["question"],
        "additionalProperties": False,
    },
}


class Worker:
    def __init__(self, manager, agent, boundary_hook=None, provider_verified=False):
        if set(tool_registry) - set(TTL):
            raise RuntimeErrorBase("tool_evidence_policy_missing")
        self.manager = manager
        self.store = manager.store
        self.agent = agent
        self.owner = "worker-" + uuid4().hex
        self.boundary_hook = boundary_hook
        self.provider_verified = provider_verified
        self.stop_requested = lambda: False

    def _boundary(self, phase, session_id):
        # Fault-injection seam only; no production process termination feature.
        if self.boundary_hook is not None:
            self.boundary_hook(phase, session_id)

    def run_once(self):
        self.manager.recover()
        with self.store.read() as db:
            if self.store.metadata(db, "model_blocked"):
                return None
        session = self.store.claim(self.owner, self.manager.limits)
        if session is None:
            return None
        claim = (self.owner, session.fencing_token)
        checkpoint = self.store.load_checkpoint(session)
        phase = "run"
        failure_category = None
        try:
            try:
                self._run(session.session_id, claim, checkpoint)
            except LostClaim:
                raise
            except (sqlite3.DatabaseError, RuntimeIntegrityError, ValidationError, json.JSONDecodeError):
                raise
            except Exception as exc:
                phase = "failure"
                failure_category = type(exc).__name__
                self._failure(session.session_id, claim, checkpoint, exc)
            phase = "projection"
            self.manager.sync_incident(session.session_id, claim=claim)
        except LostClaim as exc:
            # Audit only: never publish this owner's in-memory checkpoint/evidence.
            with self.store.transaction() as db:
                self.store.event(db, session.session_id, "worker_claim_lost", self.owner,
                                 phase=phase, reason=str(exc), fence=claim[1],
                                 failure_category=failure_category)
            logger.warning("Worker lost claim session=%s phase=%s", session.session_id, phase)
            return self.store.get(session.session_id)
        return self.store.get(session.session_id)

    def _save(self, session_id, claim, checkpoint, event=None):
        with self.store.transaction() as db:
            session = self.store.guard(db, session_id, *claim)
            session.lease_until = self.store.clock() + timedelta(seconds=self.manager.limits.lease_seconds)
            self.store.checkpoint(session, checkpoint, db, self.manager.limits.checkpoints_to_keep)
            self.store.save(session, db)
            if event:
                self.store.event(db, session_id, event)
        return session

    def _safe(self, session_id, claim, checkpoint):
        session, changed = self.manager.process_commands(session_id, claim, checkpoint)
        if session.status == S.RUNNING and self.stop_requested():
            with self.store.transaction() as db:
                session = self.store.guard(db, session_id, *claim)
                transition(session, S.QUEUED)
                session.owner_token = None
                session.lease_until = None
                self.store.checkpoint(session, checkpoint, db, self.manager.limits.checkpoints_to_keep)
                self.store.save(session, db)
                self.store.event(db, session_id, "worker_shutdown_at_safe_point")
        incident = self.manager.incident_store.get_incident(session.incident_id, include_history=False)
        if session.status == S.RUNNING and (
            incident is None or incident.alert_state == "cleared" or incident.status == "resolved"
        ):
            self._finish(session_id, claim, checkpoint, S.CANCELLED, "alert_cleared")
            return False, changed
        if changed:
            checkpoint["history"] = []
            if checkpoint.get("pending"):
                checkpoint["pending"] = []
                checkpoint["phase"] = "ready"
                self._save(session_id, claim, checkpoint, "pending_readonly_calls_interrupted")
        return session.status == S.RUNNING, changed

    def _reserve(self, session_id, claim, checkpoint, kind):
        with self.store.transaction() as db:
            session = self.store.guard(db, session_id, *claim)
            usage = self.store.budget_usage(session, db)
            if (self.store.clock() - session.created_at).total_seconds() >= self.manager.limits.max_duration_seconds:
                raise RuntimeErrorBase("session_duration_exhausted")
            if kind == "model" and session.total_model_requests >= self.manager.limits.max_model_requests:
                raise RuntimeErrorBase("session_model_budget_exhausted")
            if kind == "tool" and session.total_tool_executions >= self.manager.limits.max_tool_executions:
                raise RuntimeErrorBase("session_tool_budget_exhausted")
            reason = budget_exhaustion(usage, self.manager.limits, self.store.clock(), resource=kind)
            if reason:
                raise RuntimeErrorBase(reason)
            if kind == "model":
                if session.current_attempt_rounds >= MAX_AGENT_LOOPS:
                    raise RuntimeErrorBase("attempt_budget_exhausted")
                session.current_attempt_rounds += 1
                session.total_model_requests += 1
            else:
                if session.current_attempt_tool_calls >= MAX_TOOL_CALLS:
                    raise AttemptToolBudgetExhausted("attempt_tool_budget_exhausted")
                session.current_attempt_tool_calls += 1
                session.total_tool_executions += 1
            checkpoint["phase"] = kind + "_started"
            checkpoint["budget"] = {
                "attempt": session.attempt_count,
                "model_requests": session.total_model_requests,
                "tool_executions": session.total_tool_executions,
            }
            self.store.checkpoint(session, checkpoint, db, self.manager.limits.checkpoints_to_keep)
            self.store.save(session, db)
            self.store.event(db, session_id, kind + "_reserved", count=checkpoint["budget"])
            return session

    def _new_attempt(self, session_id, claim, checkpoint):
        with self.store.transaction() as db:
            session = self.store.guard(db, session_id, *claim)
            if session.attempt_count == 0 or checkpoint.get("new_attempt"):
                reason = budget_exhaustion(self.store.budget_usage(session, db), self.manager.limits,
                                          self.store.clock(), new_attempt=True)
                if reason:
                    raise RuntimeErrorBase(reason)
                if session.attempt_count >= self.manager.limits.max_attempts:
                    raise RuntimeErrorBase("session_budget_exhausted")
                session.attempt_count += 1
                session.current_attempt_rounds = 0
                session.current_attempt_tool_calls = 0
                checkpoint["new_attempt"] = False
                checkpoint["pending"] = []
                checkpoint["history"] = []
                checkpoint["phase"] = "ready"
                self.store.checkpoint(session, checkpoint, db, self.manager.limits.checkpoints_to_keep)
                self.store.save(session, db)
                self.store.event(db, session_id, "attempt_started", attempt=session.attempt_count)
        return session

    def _context(self, session_id, checkpoint):
        session = self.store.get(session_id)
        incident = self.manager.incident_store.get_incident(session.incident_id, include_history=False)
        checkpoint["investigated_value"] = incident.alerts[-1].value
        checkpoint["investigated_state"] = incident.alerts[-1].state
        # Historical dynamic observations remain persisted but are not current facts.
        now = self.store.clock()
        current = effective_records(checkpoint["records"], checkpoint.get("identity"), now, session.service)
        metric_analysis = self._metric_analysis_context(
            session.service,
            checkpoint["records"],
        )
        records = current
        bounded = []
        context_bytes = 0
        keys = set()
        for record in reversed(records):
            size = len(json.dumps(record["result"]).encode("utf-8"))
            if record["cache_key"] not in keys and context_bytes + size <= 60000:
                bounded.append(record)
                keys.add(record["cache_key"])
                context_bytes += size
        records = list(reversed(bounded))
        incident.investigations = [r.model_copy(update={"answer": r.answer[:8000]})
                                   for r in incident.investigations[-3:]]
        incident.evidence = [Evidence(
            source=r["name"],
            summary="当前有效的工具观测（截至本轮上下文采集时间）",
            observation_id=r["observation_id"],
            collected_at=r["collected_at"],
            detail=json.dumps(
                self._observation_summary(r["result"], r["name"])
                if r["name"] == "query_logs" else r["result"],
                ensure_ascii=False,
            ),
        ) for r in records]
        historical = []
        missing = []
        summary_bytes = 0
        seen = set()
        for record in reversed(checkpoint["records"]):
            if record in records or record["cache_key"] in seen:
                continue
            seen.add(record["cache_key"])
            observation = {
                "observation_id": record["observation_id"],
                "name": record["name"],
                "target": record["arguments"].get("service", record["arguments"].get("dependency", session.service)),
                "arguments": record["arguments"],
                "collected_at": record["collected_at"],
                "status": record["result"].get("status"),
                "expired": not fresh(record, now),
                "use_for_current_state": False,
                "result": self._observation_summary(record["result"], record["name"]),
            }
            if record in current:
                observation["context_note"] = "current_observation_omitted_by_size_limit"
            elif fresh(record, now):
                observation["context_note"] = "reference_only_or_runtime_identity_changed"
            size = len(json.dumps(observation, ensure_ascii=False).encode("utf-8"))
            if summary_bytes + size > 20000:
                continue
            summary_bytes += size
            (historical if record["success"] and record["result"].get("status") == "ok" else missing).append(observation)
        # B still delivers call/result identities explicitly. It does not fabricate a
        # Responses chain for an unverified provider or replay unsaved tool outputs.
        outputs = {i["call_id"]: i["output"] for i in checkpoint.get("history", [])
                   if i.get("type") == "function_call_output"}
        call_results = []
        call_summary_bytes = 0
        for item in checkpoint.get("history", []):
            if item.get("type") != "function_call" or item.get("call_id") not in outputs:
                continue
            cached_reference = next((t.get("observation_id") for t in reversed(checkpoint["trace"])
                                     if t.get("call_id") == item["call_id"] and t.get("from_cache")), None)
            output = json.loads(outputs[item["call_id"]])
            observation = next((r for r in reversed(checkpoint["records"])
                                if (r["call_id"] == item["call_id"] or r["observation_id"] == cached_reference)
                                and r["name"] == item["name"] and r["result"] == output), None)
            summary = self._observation_summary(output, item["name"])
            size = len(json.dumps(summary, ensure_ascii=False).encode("utf-8"))
            if call_summary_bytes + size > 16000:
                keys = ("status", "reason")
                if item["name"] == "query_metrics":
                    keys += (
                        "metric",
                        "evaluated_at",
                        "query_started_at",
                        "query_finished_at",
                    )
                elif item["name"] == "query_metric_range":
                    keys = METRIC_RANGE_CONTEXT_FIELDS
                elif item["name"] == "query_metrics_aligned":
                    keys = METRICS_ALIGNED_CONTEXT_FIELDS
                summary = {
                    key: summary[key] for key in keys if key in summary
                } | {"summary_truncated": True}
            else:
                call_summary_bytes += size
            call_results.append({
                "call_id": item["call_id"],
                "name": item["name"],
                "result": summary,
                "observation_id": observation["observation_id"] if observation else None,
                "collected_at": observation["collected_at"] if observation else None,
                "use_for_current_state": observation in current if observation else False,
            })
        rebuilt = self.agent.build_incident_prompt(incident) + "\n" + json.dumps({
            "recovery": "基于结构化证据重建上下文，不是原模型推理的精确继续",
            "human_provided_unverified_context": checkpoint.get("messages", []),
            "historical_observations": list(reversed(historical)),
            "failed_or_missing_observations": list(reversed(missing)),
            "recent_function_call_results": call_results,
            "metric_analysis": metric_analysis,
            "evidence_usage": (
                "只有当前有效证据用于当前状态判断；time_window 历史指标或日志"
                "只说明请求区间的观测，不能证明当前健康。其他历史观测只说明"
                "采集时的状态，固定 SHA 是版本参考。人类线索尚未验证。"
                "metric_analysis 是历史派生分析，不是当前状态证据；"
                "其中的候选解释尚未验证，suggested_checks 不会自动执行。"
            ),
            "context_collected_at": now.isoformat(),
            "remaining_session_budget": {
                "models": self.manager.limits.max_model_requests - session.total_model_requests,
                "tools": self.manager.limits.max_tool_executions - session.total_tool_executions,
            },
        }, ensure_ascii=False)
        chain_fresh = all(
            r in current
            for r in checkpoint["records"]
            if r.get("result", {}).get("status") == "ok"
            or r["name"] == "query_metrics_aligned"
        )
        context, strategy = restore_context(
            checkpoint.get("history", []),
            rebuilt,
            provider_verified=self.provider_verified and chain_fresh,
        )
        if strategy == "complete_chain" and metric_analysis is not None:
            # This is new Runtime input, not a reconstructed assistant message
            # or tool output. Preserve all saved call/output pairs unchanged.
            context = context + [{
                "role": "user",
                "content": json.dumps({
                    "context_origin": "runtime_derived_metric_analysis",
                    "metric_analysis": metric_analysis,
                    "context_collected_at": now.isoformat(),
                    "evidence_usage": (
                        "历史派生分析，不是人类消息或审批，不用于当前状态判断；"
                        "候选解释尚未验证，suggested_checks 不会自动执行。"
                    ),
                }, ensure_ascii=False),
            }]
        return context

    def _metric_analysis_context(
        self,
        service,
        records,
    ):
        """Project loaded raw records only; no I/O, persistence or budget changes."""
        if not any(record["name"] in METRIC_ANALYSIS_TOOLS for record in records):
            return None
        context = {
            "status": "insufficient_evidence",
            "evidence_scope": "historical_analysis",
            "use_for_current_state": False,
            "session_service": service,
            "selection": "newest_first_whole_records",
            "limits": {
                "records": MAX_ANALYSIS_OBSERVATIONS,
                "samples": MAX_ANALYSIS_SAMPLES,
                "input_bytes": MAX_ANALYSIS_INPUT_BYTES,
                "summary_bytes": MAX_METRIC_ANALYSIS_CONTEXT_BYTES,
            },
            "selected_record_count": 0,
            "selected_sample_slots": 0,
            "selected_input_bytes": 2,
            "excluded_record_counts": {
                "unauthorized_target": 0,
                "target_mismatch": 0,
                "record_limit": 0,
                "sample_limit": 0,
                "input_bytes_limit": 0,
            },
            "reports": [],
        }
        try:
            permitted = {service, *registry.dependencies(service)}
            grouped = {}
            excluded = context["excluded_record_counts"]
            for record in reversed(records):
                name = record["name"]
                if name not in METRIC_ANALYSIS_TOOLS:
                    continue
                target = record["arguments"].get("service")
                if not isinstance(target, str) or target not in permitted:
                    excluded["unauthorized_target"] += 1
                    continue
                result = record["result"]
                if result.get("service", target) != target:
                    excluded["target_mismatch"] += 1
                    continue
                if context["selected_record_count"] >= MAX_ANALYSIS_OBSERVATIONS:
                    excluded["record_limit"] += 1
                    continue
                # Count aligned slots conservatively, including missing members.
                if name == "query_metric_range":
                    points = result.get("samples")
                elif name == "query_metrics_aligned":
                    points = result.get("results")
                else:
                    points = None
                samples = len(points) if isinstance(points, (list, dict)) else 1
                if context["selected_sample_slots"] + samples > MAX_ANALYSIS_SAMPLES:
                    excluded["sample_limit"] += 1
                    continue
                size = len(json.dumps(
                    record, ensure_ascii=False, allow_nan=False,
                ).encode("utf-8")) + 2
                if context["selected_input_bytes"] + size > MAX_ANALYSIS_INPUT_BYTES:
                    excluded["input_bytes_limit"] += 1
                    continue
                # Keep identity, scope, samples and provenance intact. The
                # analyzer validates a bounded subset, never the whole Checkpoint.
                grouped.setdefault(target, []).append(record)
                context["selected_record_count"] += 1
                context["selected_sample_slots"] += samples
                context["selected_input_bytes"] += size
            for target, selected in grouped.items():
                report = analyze_metric_evidence(selected, service=target).to_dict()
                context["reports"].append({
                    "service": target,
                    "target_role": "primary" if target == service else "dependency",
                    **report,
                })
            statuses = {report["status"] for report in context["reports"]}
            if statuses and statuses != {"insufficient_evidence"}:
                context["status"] = (
                    "complete" if statuses == {"complete"} and not any(excluded.values())
                    else "partial"
                )
            if not grouped:
                context["reason"] = "no_authorized_metric_records_within_limits"
            if len(json.dumps(
                context, ensure_ascii=False, allow_nan=False,
            ).encode("utf-8")) > MAX_METRIC_ANALYSIS_CONTEXT_BYTES:
                context.update(
                    status="insufficient_evidence",
                    reason="metric_analysis_context_size_limit",
                    reports=[],
                )
        except Exception as exc:
            # Only optional local analysis lives here. RuntimeStore/lease/model
            # failures remain outside this boundary and retain their semantics.
            logger.warning("Metric context analysis failed category=%s", type(exc).__name__)
            context.update(
                status="insufficient_evidence",
                reason="metric_analysis_failed",
                reports=[],
            )
        return context

    @staticmethod
    def _observation_summary(result, tool_name=None):
        """Bound summaries, including logs/source, without dropping useful scalar fields."""
        if tool_name == "query_metric_range":
            return Worker._metric_range_summary(result)
        if tool_name == "query_metrics_aligned":
            return Worker._metrics_aligned_summary(result)
        truncated = False
        def bounded(value, depth=0):
            nonlocal truncated
            if isinstance(value, dict):
                if depth >= 4:
                    truncated = True
                    return "[summary truncated]"
                truncated |= len(value) > 24
                return {key: bounded(item, depth + 1) for key, item in list(value.items())[:24]}
            if isinstance(value, list):
                truncated |= len(value) > 3
                return [bounded(item, depth + 1) for item in value[-3:]]
            if isinstance(value, str):
                truncated |= len(value) > 1000
                return value[:1000] + (" [summary truncated]" if len(value) > 1000 else "")
            return value
        protected_result = protect(result)
        if tool_name == "query_logs" and isinstance(protected_result.get("logs"), list):
            # query_logs is newest first; only trim the redacted summary copy.
            truncated = len(protected_result["logs"]) > 3
            protected_result["logs"] = protected_result["logs"][:3]
        summary = bounded(protected_result)
        if truncated:
            summary["summary_truncated"] = True
        if len(json.dumps(summary, ensure_ascii=False).encode("utf-8")) > 5000:
            keys = ("status", "reason", "service", "dependency", "value", "reachable", "ref", "commit_sha")
            if tool_name == "query_metrics":
                keys += (
                    "metric",
                    "promql",
                    "evaluated_at",
                    "query_started_at",
                    "query_finished_at",
                )
            if tool_name == "query_logs":
                keys += ("query_range", "evidence_scope", "log_count", "limit", "truncated", "out_of_range_count", "truncation_reason")
            return {key: summary[key] for key in keys if key in summary} | {"summary_truncated": True}
        return summary

    @staticmethod
    def _metrics_aligned_summary(result):
        """Keep every aligned observation; omit only the full query/label metadata."""
        protected = protect(result)
        summary = {
            key: protected[key]
            for key in METRICS_ALIGNED_CONTEXT_FIELDS
            if key in protected and key != "results"
        }
        fields = (
            "status",
            "reason",
            "metric",
            "value",
            "evaluated_at",
            "observed_evaluated_at",
            "query_started_at",
            "query_finished_at",
            "request_executed",
        )
        summary["results"] = {
            metric: {key: item[key] for key in fields if key in item}
            for metric, item in protected.get("results", {}).items()
        }
        summary["summary_truncated"] = any(
            set(item) - set(fields) for item in protected.get("results", {}).values()
        )
        return summary

    @staticmethod
    def _metric_range_summary(result):
        """Select real points across the window; never interpolate or fill gaps."""
        protected = protect(result)
        summary = {
            key: protected[key] for key in METRIC_RANGE_CONTEXT_FIELDS if key in protected
        }
        samples = protected.get("samples", [])
        if len(samples) > 12:
            indices = [index * (len(samples) - 1) // 11 for index in range(12)]
            selected = [samples[index] for index in indices]
        else:
            selected = samples
        summary.update(
            samples=selected,
            summary_point_count=len(selected),
            summary_selection="evenly_selected_observed_points_no_interpolation",
            summary_truncated=len(selected) < len(samples),
        )
        if samples:
            summary["statistics"] = {
                "min": min(sample["value"] for sample in samples),
                "max": max(sample["value"] for sample in samples),
                "first": samples[0],
                "last": samples[-1],
            }
        missing = protected.get("missing_evaluated_at", [])
        summary["missing_evaluated_at"] = missing[:3]
        summary["missing_timestamps_truncated"] = len(missing) > 3
        summary["summary_truncated"] |= len(missing) > 3
        if len(json.dumps(summary, ensure_ascii=False).encode("utf-8")) > 5000:
            summary = {
                key: summary[key] for key in METRIC_RANGE_CONTEXT_FIELDS if key in summary
            } | {"summary_truncated": True}
        return summary

    def _validate_tool(self, name, arguments, service):
        entry = tool_registry.get(name)
        if entry is None:
            raise RuntimeErrorBase("tool_not_allowed")
        schema = entry["schema"]["parameters"]
        if set(arguments) - set(schema["properties"]) or set(schema["required"]) - set(arguments):
            raise RuntimeErrorBase("invalid_tool_arguments")
        types = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
        }
        for key, value in arguments.items():
            kind = schema["properties"][key]["type"]
            if not isinstance(value, types[kind]) or (isinstance(value, bool) and kind != "boolean"):
                raise RuntimeErrorBase("invalid_tool_argument_type")
        permitted = {service, *registry.dependencies(service)}
        target = arguments.get("service", arguments.get("dependency", service))
        if target not in permitted:
            raise RuntimeErrorBase("tool_target_outside_session")

    def _tool(self, session_id, claim, checkpoint, item, force=False):
        with self.store.transaction() as db:
            session = self.store.guard(db, session_id, *claim)
            if (self.store.clock() - session.created_at).total_seconds() >= self.manager.limits.max_duration_seconds:
                raise RuntimeErrorBase("session_duration_exhausted")
        try:
            arguments = json.loads(item.arguments)
            if not isinstance(arguments, dict):
                raise RuntimeErrorBase("invalid_tool_arguments")
            self._validate_tool(item.name, arguments, session.service)
        except LostClaim:
            raise
        except (ValueError, TypeError, RuntimeErrorBase):
            with self.store.transaction() as db:
                self.store.guard(db, session_id, *claim)
                self.store.event(db, session_id, "tool_rejected", tool=item.name)
            self.agent.tool_manager.log_result(item.name, session.service, "invalid_or_unallowed_tool_call", session_id=session_id)
            return {"status": "invalid_or_unallowed_tool_call"}, False
        key = cache_key(session.service, item.name, arguments, checkpoint.get("identity"))
        target = arguments.get("service", arguments.get("dependency", session.service))
        cached = next((r for r in reversed(checkpoint["records"])
                       if r["cache_key"] == key and fresh(r, self.store.clock())), None)
        if cached is None and item.name == "get_running_version":
            # A short negative cache describes a failed probe, never successful evidence.
            cached = next((r for r in reversed(checkpoint["records"])
                           if r["cache_key"] == key and r["result"].get("status") in {"no_data", "tool_error"}
                           and 0 <= (self.store.clock() - datetime.fromisoformat(r["collected_at"])).total_seconds() < 5), None)
        if cached and not force:
            with self.store.transaction() as db:
                current = self.store.guard(db, session_id, *claim)
                current.cache_hits += 1
                self.store.save(current, db)
                self.store.event(db, session_id, "tool_cache_hit", tool=item.name)
            checkpoint["trace"].append({
                "name": item.name, "call_id": item.call_id, "from_cache": True,
                "status": cached["result"].get("status"), "observation_id": cached["observation_id"],
            })
            self.agent.tool_manager.log_result(item.name, target, cached["result"].get("status"),
                                               cached["result"].get("reason"), cached=True, session_id=session_id)
            return cached["result"], True
        try:
            self._reserve(session_id, claim, checkpoint, "tool")
        except AttemptToolBudgetExhausted:
            # No observation or execution counter for a call that never ran.
            with self.store.transaction() as db:
                self.store.guard(db, session_id, *claim)
                self.store.event(db, session_id, "tool_budget_rejected", tool=item.name,
                                 call_id=item.call_id, reason="attempt_tool_budget_exhausted")
            checkpoint["trace"].append({
                "name": item.name,
                "call_id": item.call_id,
                "status": "tool_limit_reached",
                "executed": False,
                "from_cache": False,
            })
            self.agent.tool_manager.log_result(item.name, target, "tool_limit_reached",
                                               "attempt_tool_budget_exhausted", session_id=session_id)
            return {"status": "tool_limit_reached", "reason": "attempt_tool_budget_exhausted"}, False
        self._boundary("tool_before_io", session_id)
        success, _, raw = self.agent.tool_manager.run_tool(item, session_id=session_id)
        self._boundary("tool_after_io", session_id)
        result = protect(raw) if isinstance(raw, dict) else {"status": "invalid_tool_result"}
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 65536:
            result = {
                "status": "no_data",
                "reason": "runtime_tool_payload_size_limit",
            }
            success = False
        if item.name == "get_running_version" and result.get("status") == "ok":
            identity = {
                "commit_sha": result.get("commit_sha"),
                "process_started_at": result.get("process_started_at"),
            }
            if identity != checkpoint.get("identity"):
                checkpoint["history"] = []
            checkpoint["identity"] = identity
            key = cache_key(session.service, item.name, arguments, identity)
        elif item.name == "get_running_version":
            checkpoint["identity"] = {"unverified_instance": uuid4().hex}
            checkpoint["history"] = []
            key = cache_key(session.service, item.name, arguments, checkpoint["identity"])
        record = {
            "observation_id": "obs-" + uuid4().hex,
            "name": item.name,
            "arguments": arguments,
            "call_id": item.call_id,
            "result": result,
            "success": success,
            "collected_at": self.store.clock().isoformat(),
            "identity": checkpoint.get("identity"),
            "cache_key": key,
        }
        checkpoint["records"].append(record)
        checkpoint["trace"].append({
            "name": item.name,
            "arguments": arguments,
            "status": result.get("status"),
            "from_cache": False,
            "observation_id": record["observation_id"],
        })
        checkpoint["phase"] = "tool_saved"
        self._save(session_id, claim, checkpoint, "tool_saved" if result.get("status") == "ok" else "tool_failed")
        self._boundary("tool_checkpoint_committed", session_id)
        return result, False

    def _run(self, session_id, claim, checkpoint):
        session = self._new_attempt(session_id, claim, checkpoint)
        alive, changed = self._safe(session_id, claim, checkpoint)
        if not alive:
            return
        if checkpoint.get("result") and not changed:
            self._finish(session_id, claim, checkpoint, S.COMPLETED, "investigation_completed")
            return
        if checkpoint.get("phase") in {"model_started", "tool_started"}:
            self._save(session_id, claim, checkpoint, "uncertain_io_on_recovery")
        # Conservative B on restart: abandon unfinished read-only calls, retain saved outputs.
        if checkpoint.get("pending"):
            checkpoint["pending"] = []
            checkpoint["history"] = []
            self._save(session_id, claim, checkpoint, "unfinished_calls_abandoned_on_recovery")
        profile = registry.get(session.service)
        if profile and profile.runtime and session.current_attempt_tool_calls < MAX_TOOL_CALLS:
            self._tool(session_id, claim, checkpoint, SimpleNamespace(
                name="get_running_version",
                arguments=json.dumps({"service": session.service}),
                call_id="runtime-probe-" + uuid4().hex,
            ), force=True)
        while True:
            alive, changed = self._safe(session_id, claim, checkpoint)
            if not alive:
                return
            # B is also used between rounds: expired observations and instance changes
            # cannot survive inside an otherwise syntactically valid message chain.
            history = self._context(session_id, checkpoint)
            session = self._reserve(session_id, claim, checkpoint, "model")
            with self.store.read() as db:
                usage = self.store.budget_usage(session, db)
            self._boundary("model_before_io", session_id)
            response = self.agent.request_durable_response(
                history,
                session.current_attempt_rounds,
                session.current_attempt_tool_calls,
                [HUMAN_TOOL] if usage["human_requests"] < self.manager.limits.max_human_requests else [],
                self.manager.limits.model_timeout_seconds,
                remaining_tools=max(0, self.manager.limits.max_tool_executions - usage["tool_executions"]),
                remaining_rounds=max(0, self.manager.limits.max_model_requests - usage["model_requests"] + 1),
            )
            self._boundary("model_after_io", session_id)
            if getattr(response, "status", None) != "completed":
                raise RuntimeErrorBase("model_incomplete")
            calls = self.agent.get_function_calls(response)
            identifiers = [c.call_id for c in calls]
            if len(set(identifiers)) != len(identifiers) or any(not identifier for identifier in identifiers):
                raise RuntimeErrorBase("model_invalid_call_ids")
            human_questions = {}
            for call in calls:
                if call.name == "request_human_input":
                    try:
                        human_questions[call.call_id] = HumanInputArguments.model_validate_json(call.arguments).question
                    except ValidationError:
                        # Reject malformed JSON/shape/value before any raw input
                        # enters checkpoint history or exception/audit text.
                        raise RuntimeErrorBase("invalid_human_request") from None
            checkpoint["response_status"] = response.status
            checkpoint["phase"] = "response_saved"
            checkpoint["pending"] = [
                {
                    "name": c.name,
                    "arguments": c.arguments,
                    "call_id": c.call_id,
                } for c in calls
            ]
            checkpoint["history"] = protect(history + [
                i.model_dump(mode="json", exclude_none=True) for i in response.output
            ])
            if not calls:
                result = self.agent.handle_final_answer(response, session.current_attempt_rounds)
                if not result.answer.strip():
                    raise RuntimeErrorBase("model_empty_answer")
                checkpoint["result"] = protect(result.model_dump(mode="json"))
            self._save(session_id, claim, checkpoint, "model_response_saved")
            self._boundary("model_checkpoint_committed", session_id)
            alive, changed = self._safe(session_id, claim, checkpoint)
            if not alive:
                return
            if changed:
                checkpoint.pop("result", None)
                history = self._context(session_id, checkpoint)
                continue
            if not calls:
                self._finish(session_id, claim, checkpoint, S.COMPLETED, "investigation_completed")
                return
            history = checkpoint["history"]
            for call in calls:
                alive, changed = self._safe(session_id, claim, checkpoint)
                if not alive:
                    return
                if changed:
                    history = self._context(session_id, checkpoint)
                    break
                if call.name == "request_human_input":
                    checkpoint["pending"] = []
                    checkpoint["history"] = []
                    checkpoint["phase"] = "waiting_human"
                    self.manager.request_human(session_id, claim, human_questions[call.call_id], checkpoint)
                    return
                result, _ = self._tool(session_id, claim, checkpoint, call)
                history.append(self.agent.tool_manager.build_output_item(call, result))
                checkpoint["pending"] = [p for p in checkpoint["pending"] if p["call_id"] != call.call_id]
                checkpoint["history"] = history
                self._save(session_id, claim, checkpoint)
                alive, changed = self._safe(session_id, claim, checkpoint)
                if not alive:
                    return
                if changed:
                    history = self._context(session_id, checkpoint)
                    break
            checkpoint["phase"] = "ready"
            self._save(session_id, claim, checkpoint)

    def _finish(self, session_id, claim, checkpoint, target, event):
        if target == S.COMPLETED:
            if not self._refresh_proposal_evidence(session_id, claim, checkpoint):
                return
            # Every refresh exit, including missing evidence/budget, must deliver
            # controls before completion. A failed probe still yields a safe point.
            alive, changed = self._safe(session_id, claim, checkpoint)
            if not alive:
                return
            if changed:
                checkpoint.pop("result", None)
                self._yield(session_id, claim, checkpoint, "human_context_changed_before_completion")
                return
        with self.store.transaction() as db:
            session = self.store.guard(db, session_id, *claim)
            transition(session, target)
            session.completed_at = self.store.clock()
            session.owner_token = None
            session.lease_until = None
            self.store.checkpoint(session, checkpoint, db, self.manager.limits.checkpoints_to_keep)
            self.store.save(session, db)
            self.store.event(db, session_id, event)

    def _refresh_proposal_evidence(self, session_id, claim, checkpoint):
        planner = self.manager.remediation_planner
        if planner is None or not checkpoint.get("result", {}).get("success"):
            return True
        session = self.store.get(session_id)
        with self.store.read() as db:
            usage = self.store.budget_usage(session, db)
        lifecycle_limit = budget_exhaustion(usage, self.manager.limits, self.store.clock(), resource="lifecycle")
        if lifecycle_limit:
            checkpoint["proposal_evidence_gap"] = lifecycle_limit
            return True
        incident = self.manager.incident_store.get_incident(session.incident_id)
        # A historical candidate is only a reason to collect more evidence; never an approval.
        observations = [r for r in checkpoint["records"] if r["result"].get("status") == "ok"]
        evidence = [Evidence(source=r["name"], summary="历史候选观测",
                             detail=json.dumps(r["result"], ensure_ascii=False)) for r in observations]
        from remediation.state import RemediationError
        try:
            candidate = planner.plan(incident, InvestigationResult.model_validate(checkpoint["result"]), evidence)
        except (RemediationError, OSError, ValueError) as exc:
            checkpoint["proposal_evidence_gap"] = "proposal_planning_" + type(exc).__name__
            self._save(session_id, claim, checkpoint, "proposal_refresh_failed")
            return True
        if candidate is None:
            return True
        names = ("get_running_version", "query_logs", "check_dependency_health")
        required = []
        for name in names:
            record = next((r for r in reversed(observations) if r["name"] == name), None)
            if record and (name == "get_running_version" or not fresh(record, self.store.clock())):
                required.append(record)
        with self.store.read() as db:
            usage = self.store.budget_usage(session, db)
        available = min(MAX_TOOL_CALLS - session.current_attempt_tool_calls,
                        self.manager.limits.max_tool_executions - usage["tool_executions"])
        if len(required) > available:
            checkpoint["proposal_evidence_gap"] = "proposal_refresh_budget_unavailable"
            with self.store.transaction() as db:
                self.store.guard(db, session_id, *claim)
                self.store.event(db, session_id, "proposal_refresh_budget_unavailable")
            return True
        for record in required:
            alive, changed = self._safe(session_id, claim, checkpoint)
            if not alive:
                return False
            if changed:
                checkpoint.pop("result", None)
                self._yield(session_id, claim, checkpoint, "human_context_changed_before_completion")
                return False
            try:
                result, _ = self._tool(session_id, claim, checkpoint, SimpleNamespace(
                    name=record["name"], arguments=json.dumps(record["arguments"]),
                    call_id="proposal-refresh-" + uuid4().hex,
                ), force=True)
            except LostClaim:
                raise
            except RuntimeErrorBase as exc:
                if str(exc) not in {"session_duration_exhausted", "session_tool_budget_exhausted",
                                    "attempt_tool_budget_exhausted", "incident_duration_exhausted",
                                    "incident_tool_budget_exhausted", "incident_resume_budget_exhausted"}:
                    raise
                checkpoint["proposal_evidence_gap"] = str(exc)
                self._save(session_id, claim, checkpoint, "proposal_refresh_failed")
                return True
            if result.get("status") != "ok":
                checkpoint["proposal_evidence_gap"] = "proposal_refresh_missing_observation"
                self._save(session_id, claim, checkpoint, "proposal_refresh_failed")
                return True
        return True

    def _yield(self, session_id, claim, checkpoint, event):
        with self.store.transaction() as db:
            session = self.store.guard(db, session_id, *claim)
            transition(session, S.QUEUED)
            session.queued_at = self.store.clock()
            session.owner_token = None
            session.lease_until = None
            self.store.checkpoint(session, checkpoint, db, self.manager.limits.checkpoints_to_keep)
            self.store.save(session, db)
            self.store.event(db, session_id, event)

    def _failure(self, session_id, claim, checkpoint, exc):
        status = getattr(exc, "status_code", None)
        category = f"model_http_{status}" if status else (
            str(exc) if isinstance(exc, RuntimeErrorBase) else type(exc).__name__
        )
        transient = status == 429 or (status is not None and status >= 500) or isinstance(
            exc, (openai.APIConnectionError, TimeoutError)
        ) or category == "model_incomplete"
        with self.store.transaction() as db:
            session = self.store.guard(db, session_id, *claim)
            session.last_error_category = category
            if status in {401, 402, 403}:
                target = S.PAUSED
                self.store.metadata(db, "model_blocked", category, write=True)
                event = "model_blocked"
            elif (category.startswith("incident_") and category.endswith("exhausted")) or category in {
                "session_budget_exhausted", "session_duration_exhausted",
                "session_model_budget_exhausted", "session_tool_budget_exhausted",
                "attempt_budget_exhausted",
            } or exhausted(session, self.manager.limits, self.store.clock()) or budget_exhaustion(
                self.store.budget_usage(session, db), self.manager.limits, self.store.clock(),
            ):
                target = S.EXHAUSTED
                event = "budget_exhausted"
            elif transient and self.store.budget_usage(session, db)["attempts"] < self.manager.limits.max_attempts:
                target = S.WAITING_RETRY
                session.next_retry_at = self.store.clock() + timedelta(
                    seconds=self.manager.limits.retry_base_seconds * 2 ** (session.attempt_count - 1)
                )
                checkpoint["new_attempt"] = True
                event = "waiting_retry"
            else:
                target = S.EXHAUSTED if transient else S.FAILED
                event = "investigation_failed"
            transition(session, target)
            session.owner_token = None
            session.lease_until = None
            if target in {S.EXHAUSTED, S.FAILED}:
                session.completed_at = self.store.clock()
                if not checkpoint.get("result"):
                    checkpoint["result"] = {
                        "success": False,
                        "status": (InvestigationStatus.MAX_ROUND_REACHED.value
                                   if category == "attempt_budget_exhausted" else InvestigationStatus.MODEL_INCOMPLETE.value),
                        "answer": "调查停止：" + category,
                    }
            self.store.checkpoint(session, checkpoint, db, self.manager.limits.checkpoints_to_keep)
            self.store.save(session, db)
            self.store.event(db, session_id, event, category=category)
        logger.error("调查状态更新 session=%s status=%s reason=%s", session_id, target.value, category)
