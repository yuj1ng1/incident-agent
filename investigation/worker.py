"""Single synchronous Worker: durable I/O boundaries and cooperative interruption."""
from datetime import timedelta
import json
from types import SimpleNamespace
from uuid import uuid4

import openai

from config import MAX_AGENT_LOOPS, MAX_TOOL_CALLS
from investigation.checkpoint import protect, restore_context, valid_chain
from investigation.evidence_policy import cache_key, effective_records, fresh, stable
from investigation.models import SessionStatus as S
from investigation.state import LostClaim, RuntimeErrorBase, exhausted, transition
from models.incident import Evidence
from models.investigation import InvestigationResult, InvestigationStatus
from services.registry import registry
from tools.tool_registry import tool_registry

HUMAN_TOOL = {
    "type": "function",
    "name": "request_human_input",
    "description": "请求人类提供现有只读工具无法获得的必要背景；暂停调查等待回答，不具备审批权限。",
    "parameters": {
        "type": "object",
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
        "additionalProperties": False,
    },
}


class Worker:
    def __init__(self, manager, agent, boundary_hook=None, provider_verified=False):
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
        with self.store.transaction() as db:
            if self.store.metadata(db, "model_blocked"):
                return None
        session = self.store.claim(self.owner, self.manager.limits)
        if session is None:
            return None
        claim = (self.owner, session.fencing_token)
        checkpoint = self.store.load_checkpoint(session)
        try:
            self._run(session.session_id, claim, checkpoint)
        except LostClaim:
            # The expired owner cannot checkpoint, publish evidence or change status.
            return self.store.get(session.session_id)
        except Exception as exc:
            self._failure(session.session_id, claim, checkpoint, exc)
        self.manager.sync_incident(session.session_id)
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
        incident = self.manager.incident_store.get_incident(session.incident_id)
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
            if (
                (self.store.clock() - session.created_at).total_seconds() >= self.manager.limits.max_duration_seconds
                or (kind == "model" and session.total_model_requests >= self.manager.limits.max_model_requests)
                or (kind == "tool" and session.total_tool_executions >= self.manager.limits.max_tool_executions)
            ):
                raise RuntimeErrorBase("session_budget_exhausted")
            if kind == "model":
                if session.current_attempt_rounds >= MAX_AGENT_LOOPS:
                    raise RuntimeErrorBase("attempt_budget_exhausted")
                session.current_attempt_rounds += 1
                session.total_model_requests += 1
            else:
                if session.current_attempt_tool_calls >= MAX_TOOL_CALLS:
                    raise RuntimeErrorBase("attempt_tool_budget_exhausted")
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
        incident = self.manager.incident_store.get_incident(session.incident_id)
        checkpoint["investigated_value"] = incident.alerts[-1].value
        checkpoint["investigated_state"] = incident.alerts[-1].state
        # Historical dynamic observations remain persisted but are not current facts.
        records = effective_records(checkpoint["records"], checkpoint.get("identity"), self.store.clock())
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
            summary="当前有效的工具观测",
            detail=json.dumps(r["result"], ensure_ascii=False),
        ) for r in records]
        rebuilt = self.agent.build_incident_prompt(incident) + "\n" + json.dumps({
            "recovery": "基于结构化证据重建上下文，不是原模型推理的精确继续",
            "human_provided_unverified_context": checkpoint.get("messages", []),
            "tool_failures_and_historical_observations": [
                {
                    "name": r["name"],
                    "status": r["result"].get("status"),
                    "collected_at": r["collected_at"],
                    "current": fresh(r, self.store.clock()),
                }
                for r in checkpoint["records"] if r not in records
            ],
            "remaining_session_budget": {
                "models": self.manager.limits.max_model_requests - session.total_model_requests,
                "tools": self.manager.limits.max_tool_executions - session.total_tool_executions,
            },
        }, ensure_ascii=False)
        chain_fresh = all(
            stable(r["name"], r["arguments"])
            or (fresh(r, self.store.clock()) and r.get("identity") == checkpoint.get("identity"))
            for r in checkpoint["records"] if r.get("result", {}).get("status") == "ok"
        )
        return restore_context(
            checkpoint.get("history", []),
            rebuilt,
            provider_verified=self.provider_verified and chain_fresh,
        )[0]

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
        session = self.store.get(session_id)
        try:
            arguments = json.loads(item.arguments)
            if not isinstance(arguments, dict):
                raise RuntimeErrorBase("invalid_tool_arguments")
            self._validate_tool(item.name, arguments, session.service)
        except (ValueError, TypeError, RuntimeErrorBase):
            with self.store.transaction() as db:
                self.store.guard(db, session_id, *claim)
                self.store.event(db, session_id, "tool_rejected", tool=item.name)
            return {"status": "invalid_or_unallowed_tool_call"}, False
        key = cache_key(session.service, item.name, arguments, checkpoint.get("identity"))
        cached = next((r for r in reversed(checkpoint["records"])
                       if r["cache_key"] == key and fresh(r, self.store.clock())), None)
        if cached and not force:
            with self.store.transaction() as db:
                current = self.store.guard(db, session_id, *claim)
                current.cache_hits += 1
                self.store.save(current, db)
                self.store.event(db, session_id, "tool_cache_hit", tool=item.name)
            return cached["result"], True
        self._reserve(session_id, claim, checkpoint, "tool")
        self._boundary("tool_before_io", session_id)
        success, _, raw = self.agent.tool_manager.run_tool(item)
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
        history = self._context(session_id, checkpoint)
        while True:
            alive, changed = self._safe(session_id, claim, checkpoint)
            if not alive:
                return
            # B is also used between rounds: expired observations and instance changes
            # cannot survive inside an otherwise syntactically valid message chain.
            history = self._context(session_id, checkpoint)
            session = self._reserve(session_id, claim, checkpoint, "model")
            self._boundary("model_before_io", session_id)
            response = self.agent.request_durable_response(
                history,
                session.current_attempt_rounds,
                session.current_attempt_tool_calls,
                [HUMAN_TOOL] if session.human_request_count < self.manager.limits.max_human_requests else [],
                self.manager.limits.model_timeout_seconds,
            )
            self._boundary("model_after_io", session_id)
            if getattr(response, "status", None) != "completed":
                raise RuntimeErrorBase("model_incomplete")
            calls = self.agent.get_function_calls(response)
            identifiers = [c.call_id for c in calls]
            if len(set(identifiers)) != len(identifiers) or any(not identifier for identifier in identifiers):
                raise RuntimeErrorBase("model_invalid_call_ids")
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
                    arguments = json.loads(call.arguments)
                    if set(arguments) != {"question"}:
                        raise RuntimeErrorBase("invalid_human_request")
                    checkpoint["pending"] = []
                    checkpoint["history"] = []
                    checkpoint["phase"] = "waiting_human"
                    self.manager.request_human(session_id, claim, arguments["question"], checkpoint)
                    return
                result, cached = self._tool(session_id, claim, checkpoint, call)
                if cached:
                    checkpoint["trace"].append({
                        "name": call.name,
                        "from_cache": True,
                        "status": result.get("status"),
                    })
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
        if (self.store.clock() - session.created_at).total_seconds() >= self.manager.limits.max_duration_seconds:
            return True
        incident = self.manager.incident_store.get_incident(session.incident_id)
        # A historical candidate is only a reason to collect more evidence; never an approval.
        observations = [r for r in checkpoint["records"] if r["result"].get("status") == "ok"]
        evidence = [Evidence(source=r["name"], summary="历史候选观测",
                             detail=json.dumps(r["result"], ensure_ascii=False)) for r in observations]
        candidate = planner.plan(incident, InvestigationResult.model_validate(checkpoint["result"]), evidence)
        if candidate is None:
            return True
        names = ("get_running_version", "query_logs", "check_dependency_health")
        required = []
        for name in names:
            record = next((r for r in reversed(observations) if r["name"] == name), None)
            if record and (name == "get_running_version" or not fresh(record, self.store.clock())):
                required.append(record)
        available = min(MAX_TOOL_CALLS - session.current_attempt_tool_calls,
                        self.manager.limits.max_tool_executions - session.total_tool_executions)
        if len(required) > available:
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
            self._tool(session_id, claim, checkpoint, SimpleNamespace(
                name=record["name"], arguments=json.dumps(record["arguments"]),
                call_id="proposal-refresh-" + uuid4().hex,
            ), force=True)
        alive, changed = self._safe(session_id, claim, checkpoint)
        if alive and changed:
            checkpoint.pop("result", None)
            self._yield(session_id, claim, checkpoint, "human_context_changed_before_completion")
            return False
        return alive

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
        ) or category in {"model_incomplete", "attempt_budget_exhausted", "attempt_tool_budget_exhausted"}
        with self.store.transaction() as db:
            session = self.store.guard(db, session_id, *claim)
            session.last_error_category = category
            if status in {401, 402, 403}:
                target = S.PAUSED
                self.store.metadata(db, "model_blocked", category, write=True)
                event = "model_blocked"
            elif category == "session_budget_exhausted" or exhausted(session, self.manager.limits, self.store.clock()):
                target = S.EXHAUSTED
                event = "budget_exhausted"
            elif transient and session.attempt_count < self.manager.limits.max_attempts:
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
                checkpoint["result"] = {
                    "success": False,
                    "status": InvestigationStatus.MODEL_INCOMPLETE.value,
                    "answer": "调查停止：" + category,
                }
            self.store.checkpoint(session, checkpoint, db, self.manager.limits.checkpoints_to_keep)
            self.store.save(session, db)
            self.store.event(db, session_id, event, category=category)
