"""One background Worker leaves monitoring free while a session waits for humans."""
import logging
import threading
import sqlite3

from pydantic import ValidationError

from investigation.checkpoint import protect
from investigation.models import InvestigationSession, SessionStatus as S
from investigation.state import LostClaim, RuntimeErrorBase, TERMINAL


class Scheduler:
    def __init__(self, worker, interval=1):
        self.worker = worker
        self.interval = interval
        self.stop_requested = threading.Event()
        self.worker.stop_requested = self.stop_requested.is_set
        self.thread = None

    def tick(self):
        result = self.worker.run_once()
        self._notify_states()
        return result

    def _notify_states(self):
        manager = self.worker.manager
        for session in manager.store.pending_work("notification"):
            if session.status not in TERMINAL | {S.WAITING_HUMAN, S.PAUSED}:
                continue
            workflow = manager.workflow(session.session_id, include_remediations=False)
            session = InvestigationSession.model_validate(workflow["session"])
            if session.status not in TERMINAL | {S.WAITING_HUMAN, S.PAUSED}:
                continue
            pending = [r for r in workflow["human_requests"] if r["status"] == "pending"]
            signature = ":".join((session.status.value, session.last_error_category or "",
                                  pending[0]["request_id"] if pending else "",
                                  workflow["projection_error_category"] or "",
                                  "blocked" if workflow["projection_blocked"] else "",
                                  workflow["remediation_status"],
                                  workflow["remediation_error_category"] or "",
                                  workflow["remediation_blocked_reason"] or "",
                                  "persistent" if workflow["remediation_attempts"] >= 3 else ""))
            key = "terminal_notification:" + session.session_id
            with manager.store.read() as db:
                if manager.store.metadata(db, key) == signature:
                    displayed = True
                else:
                    displayed = False
            if displayed:
                with manager.store.transaction() as db:
                    latest = manager.store.get(session.session_id, db)
                    if latest.state_version == session.state_version:
                        manager.store.acknowledge_work(db, "notification", session.session_id)
                continue
            lines = [
                "[Remediation Proposal Status]" if workflow["remediation_pending"] else (
                    "[Investigation Completed]" if session.status == S.COMPLETED else (
                        "[Human Input Required]" if session.status == S.WAITING_HUMAN else "[Investigation Status]"
                    )
                ),
                "", f"Incident: {session.incident_id}", f"Session: {session.session_id}",
                f"Service: {session.service}", f"Status: {session.status.value}",
                f"Model rounds: {session.total_model_requests}", f"Tool calls: {session.total_tool_executions}",
            ]
            if session.last_error_category:
                lines.append("Last error: " + session.last_error_category)
            if pending:
                lines.extend(["Request: " + pending[0]["request_id"], "", "问题：", pending[0]["question"]])
            result = workflow["investigation_result"]
            if result and result.get("answer"):
                lines.extend(["", "调查结论：", result["answer"]])
            if workflow["proposal_evidence_gap"]:
                lines.append("修复补证缺口：" + workflow["proposal_evidence_gap"])
            if workflow["projection_error_category"]:
                prefix = "Incident 投影已隔离，需人工核查：" if workflow["projection_blocked"] else "Incident 投影待重试："
                lines.append(prefix + workflow["projection_error_category"])
            if workflow["remediation_pending"]:
                lines.extend([
                    "修复提案生成状态：" + workflow["remediation_status"],
                    "提案累计尝试：" + str(workflow["remediation_attempts"]),
                    "提案错误类别：" + (workflow["remediation_error_category"] or "unknown"),
                    "提案下次重试：" + (workflow["remediation_retry_at"] or "none"),
                ])
                if workflow["remediation_blocked"]:
                    lines.append("提案需人工处理：" + workflow["remediation_blocked_reason"])
                elif workflow["remediation_attempts"] >= 3:
                    lines.append("提案持续失败，需操作员核查；调查结论已保存。")
            try:
                # The displayed snapshot is already committed; stdout holds no DB lock.
                print(protect("\n".join(lines)) + "\n", flush=True)
            except (OSError, UnicodeError) as exc:
                logging.getLogger(__name__).warning("Runtime display unavailable category=%s", type(exc).__name__)
                continue
            with manager.store.transaction() as db:
                latest = manager.store.get(session.session_id, db)
                if latest.state_version != session.state_version:
                    continue
                manager.store.metadata(db, key, signature, write=True)
                manager.store.acknowledge_work(db, "notification", session.session_id)
                manager.store.event(db, session.session_id, "operator_status_displayed", status=session.status.value)

    def run(self):
        while not self.stop_requested.is_set():
            try:
                result = self.tick()
            except LostClaim:
                logging.getLogger(__name__).warning("Runtime claim lost; continuing with durable state")
                result = None
            except (sqlite3.DatabaseError, RuntimeErrorBase, ValidationError, ValueError) as exc:
                logging.getLogger(__name__).error("Runtime stopped; store consistency failure category=%s", type(exc).__name__)
                self.stop_requested.set()
                break
            except Exception:
                logging.getLogger(__name__).exception("Runtime scheduler failed; durable state retained")
                result = None
            if result is None:
                self.stop_requested.wait(self.interval)

    def start(self):
        if self.thread is not None:
            raise RuntimeError("scheduler_already_started")
        self.thread = threading.Thread(target=self.run, name="investigation-worker", daemon=False)
        self.thread.start()

    def stop(self):
        self.stop_requested.set()
        if self.thread is not None:
            self.thread.join()
