"""Application service shared by local CLI, Monitor and future authenticated Web."""
from datetime import datetime, timedelta
from contextlib import nullcontext
import json
from uuid import uuid4

from config import ALERT_POLL_INTERVAL_SECONDS, RUNTIME_LIMITS
from investigation.checkpoint import encode, protect
from investigation.models import HumanCommand, HumanRequest, InvestigationSession, RuntimeLimits, SessionStatus as S
from investigation.state import LostClaim, RuntimeErrorBase, RuntimeIntegrityError, TERMINAL, budget_exhaustion, exhausted, transition
from incident_store import RuntimeProjectionResult
from reinvestigation import reinvestigation_reason, utc_timestamp
from storage_lock import FileLockError

REMEDIATION_MAX_ATTEMPTS = 6
REMEDIATION_RETRY_MAX_SECONDS = 300


def _remediation_failure(exc):
    """Persist controlled codes/classes only, never exception text or user data."""
    from remediation.store import StoreError
    if isinstance(exc, StoreError):
        code = str(exc)
        if code in {"store_busy", "remediation_store_write_failed", "remediation_store_corrupt_or_unreadable"}:
            return code, code != "remediation_store_corrupt_or_unreadable"
        return "StoreError", False
    if isinstance(exc, PermissionError):
        return "PermissionError", False
    return type(exc).__name__, isinstance(exc, (OSError, FileLockError))


def live_alert_for_incident(prometheus_client, incident):
    from alert_parser import normalize_snapshot
    from fingerprint import generate_alert_fingerprint
    snapshot = normalize_snapshot(prometheus_client.get_alerts())
    if not snapshot.complete:
        raise ValueError("untrusted_live_alert_snapshot")
    return next((alert for alert in snapshot.alerts
                 if generate_alert_fingerprint(alert) == incident.fingerprint), None)


class InvestigationManager:
    def __init__(self, store, incident_store, remediation_planner=None, limits=None, alert_verifier=None):
        self.store = store
        self.incident_store = incident_store
        self.remediation_planner = remediation_planner
        self.limits = limits or RuntimeLimits(**RUNTIME_LIMITS)
        self.alert_verifier = alert_verifier

    def enqueue(self, incident, reinvestigate=False):
        incident = self.incident_store.get_incident(incident.incident_id, include_history=False)
        if incident is None:
            raise RuntimeErrorBase("incident_not_found")
        with self.store.read() as db:
            latest = self.store.latest_session(incident.incident_id, db)
            reason = self._enqueue_reason(incident, latest, db, reinvestigate)
            if reason and self.store.metadata(db, "reinvestigation_refusal:" + incident.incident_id) == reason:
                return latest
        with self.store.transaction() as db:
            latest = self.store.latest_session(incident.incident_id, db)
            reason = self._enqueue_reason(incident, latest, db, reinvestigate)
            key = "reinvestigation_refusal:" + incident.incident_id
            if reason:
                if self.store.metadata(db, key) != reason:
                    self.store.metadata(db, key, reason, write=True)
                    if latest:
                        self.store.event(db, latest.session_id, "reinvestigation_rejected", "monitor", reason=reason)
                return latest
            self.store.metadata(db, key, None, write=True)
            return self._insert_session(incident, latest, db)

    def _enqueue_reason(self, incident, latest, db, requested):
        if latest and not requested:
            return "reinvestigation_not_requested"
        if not incident.alerts:
            return "incident_has_no_alert"
        reason = reinvestigation_reason(incident, incident.alerts[-1], self.store.clock(), latest)
        if reason:
            return reason
        if latest:
            checkpoint = self.store.load_checkpoint(latest, db)
            if not checkpoint.get("projected"):
                return "previous_projection_pending"
            reason = budget_exhaustion(self.store.budget_usage(latest, db), self.limits,
                                      self.store.clock(), new_attempt=True)
            if reason:
                return reason
            if not self._fresh_alert(incident.alerts[-1]):
                return "fresh_alert_observation_required"
        return None

    def _fresh_alert(self, alert):
        if alert is None or alert.state != "firing" or alert.observed_at is None:
            return False
        age = (self.store.clock() - utc_timestamp(alert.observed_at)).total_seconds()
        return -5 <= age <= 2 * ALERT_POLL_INTERVAL_SECONDS

    def _insert_session(self, incident, previous, db, command=None):
        now = self.store.clock()
        session = InvestigationSession(
            incident_id=incident.incident_id,
            alert_fingerprint=incident.fingerprint,
            service=incident.service,
            budget_group_id=previous.budget_group_id if previous else None,
            priority=1 if incident.severity.lower() == "critical" else 0,
            created_at=now, updated_at=now, queued_at=now,
        )
        if command:
            session.budget_group_id = session.session_id
        self.store.insert(session, db)
        transition(session, S.QUEUED)
        self.store.save(session, db)
        self.store.event(db, session.session_id, "queued", command.actor if command else "monitor",
                         previous_session_id=previous.session_id if previous else None,
                         budget_group_id=session.budget_group_id)
        return session

    def submit(self, command):
        command = HumanCommand.model_validate(command)
        if command.processing_status != "pending" or command.processed_at is not None:
            raise RuntimeErrorBase("new_command_must_be_pending")
        payload = command.payload
        if command.command_type in {"message", "answer"}:
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                raise RuntimeErrorBase("message_text_required_max_4000")
        if command.command_type == "reopen":
            reason = payload.get("reason")
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
                raise RuntimeErrorBase("reopen_reason_required_max_2000")
        allowed = {"text"} if command.command_type == "message" else (
            {"text", "request_id"} if command.command_type == "answer" else (
                {"reason"} if command.command_type == "reopen" else set()
            )
        )
        if set(payload) != allowed:
            raise RuntimeErrorBase("invalid_command_payload")
        command.payload = protect(payload)
        with self.store.transaction() as db:
            self.store.get(command.session_id, db)
            old = db.execute(
                "SELECT data FROM investigation_commands WHERE command_id=?", (command.command_id,)
            ).fetchone()
            if old:
                existing = HumanCommand.model_validate_json(old[0])
                fields = ("session_id", "actor", "command_type", "payload", "expected_version")
                if any(getattr(existing, f) != getattr(command, f) for f in fields):
                    raise RuntimeErrorBase("command_id_conflict")
                return existing
            count = db.execute(
                "SELECT count(*) FROM investigation_commands WHERE session_id=?", (command.session_id,)
            ).fetchone()[0]
            if count >= 100:
                raise RuntimeErrorBase("session_command_limit")
            if command.command_type in {"message", "answer"}:
                texts = db.execute(
                    "SELECT json_extract(data,'$.payload.text') FROM investigation_commands WHERE session_id=?",
                    (command.session_id,),
                ).fetchall()
                if sum(len((r[0] or "").encode("utf-8")) for r in texts) + len(command.payload["text"].encode("utf-8")) > 64000:
                    raise RuntimeErrorBase("human_context_size_limit")
            db.execute(
                "INSERT INTO investigation_commands (command_id,session_id,status,data) VALUES (?,?,?,?)",
                (command.command_id, command.session_id, "pending", command.model_dump_json()),
            )
            self.store.event(db, command.session_id, command.command_type + "_requested", command.actor,
                             command_id=command.command_id)
        # Persist first, then process non-running sessions. Running Worker delivers at safe points.
        self.process_commands(command.session_id)
        result = self.get_command(command.command_id)
        if result.command_type == "reopen" and result.processing_status == "applied":
            self.sync_incident(result.result)
        return result

    def get_command(self, command_id):
        with self.store.read() as db:
            row = db.execute(
                "SELECT data FROM investigation_commands WHERE command_id=?", (command_id,)
            ).fetchone()
            if not row:
                raise RuntimeErrorBase("command_not_found")
            return HumanCommand.model_validate_json(row[0])

    def _requests(self, db, session_id):
        rows = db.execute("SELECT data FROM human_requests WHERE session_id=? ORDER BY rowid", (session_id,))
        return [HumanRequest.model_validate_json(row[0]) for row in rows]

    @staticmethod
    def _put_request(db, request):
        db.execute(
            "UPDATE human_requests SET status=?,data=? WHERE request_id=?",
            (request.status, request.model_dump_json(), request.request_id),
        )

    def requests(self, session_id=None):
        with self.store.read() as db:
            if session_id:
                self.store.get(session_id, db)
                return self._requests(db, session_id)
            return [HumanRequest.model_validate_json(r[0]) for r in
                    db.execute("SELECT data FROM human_requests ORDER BY rowid")]

    def unblock_model(self, actor):
        """Explicit local operator action; cancellation never implies authentication recovery."""
        if (not isinstance(actor, str) or not actor.strip() or len(actor) > 100
                or any(ord(c) < 32 for c in actor)):
            raise RuntimeErrorBase("actor_required_max_100")
        with self.store.transaction() as db:
            category = self.store.metadata(db, "model_blocked")
            self.store.metadata(db, "model_blocked", None, write=True)
            operation = {
                "operation_id": "unblock-" + uuid4().hex,
                "actor": protect(actor), "at": self.store.clock().isoformat(), "previous_reason": category,
                "result": "unblocked" if category else "already_unblocked",
            }
            self.store.metadata(db, "last_model_unblock", operation, write=True)
            self.store.metadata(db, operation["operation_id"], operation, write=True)
            if category:
                for session in self.store.list_sessions(db):
                    if session.last_error_category == category:
                        self.store.event(db, session.session_id, "model_unblocked", protect(actor),
                                         operation_id=operation["operation_id"], at=operation["at"],
                                         previous_reason=category, result=operation["result"])
        return {"model_blocked": None, **operation}

    def process_commands(self, session_id, claim=None, payload=None):
        with self.store.read() as db:
            session = self.store.get(session_id, db)
            pending = db.execute(
                "SELECT data FROM investigation_commands WHERE session_id=? AND status='pending'", (session_id,)
            ).fetchall()
        if claim is None and (session.status == S.RUNNING or not pending):
            return session, False
        verified = {}
        for row in pending:
            command = HumanCommand.model_validate_json(row[0])
            if command.command_type == "reopen":
                verified[command.command_id] = self._verify_reopen(session)
        with self.store.transaction() as db:
            session = self.store.get(session_id, db)
            if session.status == S.RUNNING:
                if claim is None:
                    return session, False
                session = self.store.guard(db, session_id, *claim)
            checkpoint = payload if payload is not None else self.store.load_checkpoint(session, db)
            rows = db.execute(
                """SELECT data FROM investigation_commands WHERE session_id=? AND status='pending'
                    ORDER BY CASE WHEN json_extract(data,'$.command_type')='cancel' THEN 0 ELSE 1 END, sequence""",
                (session_id,),
            ).fetchall()
            changed = False
            context_changed = False
            for row in rows:
                command = HumanCommand.model_validate_json(row[0])
                try:
                    if command.expected_version is not None and command.expected_version != session.state_version:
                        raise RuntimeErrorBase("command_state_version_conflict")
                    if session.status in TERMINAL and command.command_type != "reopen":
                        raise RuntimeErrorBase("session_terminal")
                    kind = command.command_type
                    if kind == "reopen":
                        command.result = self._reopen(session, command, verified.get(command.command_id), db).session_id
                    elif kind in {"message", "answer"}:
                        if kind == "answer":
                            request = next((r for r in self._requests(db, session_id)
                                            if r.request_id == command.payload["request_id"]), None)
                            if request is None or request.status != "pending":
                                raise RuntimeErrorBase("human_request_not_pending")
                            if request.expires_at <= self.store.clock():
                                raise RuntimeErrorBase("human_request_expired")
                            if session.status not in {S.WAITING_HUMAN, S.PAUSED}:
                                raise RuntimeErrorBase("session_not_waiting_for_answer")
                            if session.status == S.WAITING_HUMAN:
                                self._resume(session, checkpoint, db)
                            request.status = "answered"
                            request.answer_command_id = command.command_id
                            self._put_request(db, request)
                            self.store.event(db, session_id, "human_request_answered", command.actor,
                                             request_id=request.request_id)
                        db.execute(
                            "INSERT INTO human_messages (command_id,session_id,data) VALUES (?,?,?)",
                            (command.command_id, session_id, encode({
                                "actor": command.actor,
                                "text": command.payload["text"],
                                "request_id": command.payload.get("request_id"),
                                "command_id": command.command_id,
                                "kind": "human_provided_unverified_context",
                            })),
                        )
                        self.store.event(db, session_id, "human_message_received", command.actor,
                                         command_id=command.command_id)
                    elif kind == "pause":
                        if session.status != S.PAUSED:
                            transition(session, S.PAUSED)
                        self.store.event(db, session_id, "paused", command.actor)
                    elif kind == "resume":
                        if session.status != S.PAUSED:
                            raise RuntimeErrorBase("resume_requires_paused_session")
                        self._resume(session, checkpoint, db)
                        self.store.event(db, session_id, "resumed", command.actor)
                    elif kind == "cancel":
                        transition(session, S.CANCELLED)
                        session.completed_at = self.store.clock()
                        for request in self._requests(db, session_id):
                            if request.status in {"pending", "answered"}:
                                request.status = "cancelled"
                                self._put_request(db, request)
                        self.store.event(db, session_id, "investigation_cancelled", command.actor)
                    command.processing_status = "applied"
                    if kind != "reopen":
                        command.result = session.status.value
                        changed = True
                except RuntimeIntegrityError:
                    raise
                except RuntimeErrorBase as exc:
                    command.processing_status = "rejected"
                    command.result = str(exc)
                command.processed_at = self.store.clock()
                db.execute(
                    "UPDATE investigation_commands SET status=?,data=? WHERE command_id=?",
                    (command.processing_status, command.model_dump_json(), command.command_id),
                )
                self.store.event(db, session_id, "command_processed", command.actor,
                                 command_id=command.command_id, result=command.result,
                                 status=command.processing_status)
            if claim is not None and session.status == S.RUNNING:
                messages = db.execute(
                    """SELECT command_id,data FROM human_messages WHERE session_id=?
                        AND consumed_at IS NULL ORDER BY sequence""", (session_id,),
                ).fetchall()
                for message in messages:
                    checkpoint.setdefault("messages", []).append(json.loads(message["data"]))
                    db.execute("UPDATE human_messages SET consumed_at=? WHERE command_id=?",
                               (self.store.clock().isoformat(), message["command_id"]))
                    self.store.event(db, session_id, "human_message_applied",
                                     command_id=message["command_id"])
                    context_changed = changed = True
                for request in self._requests(db, session_id):
                    if request.status == "answered":
                        request.status = "consumed"
                        self._put_request(db, request)
                lifecycle_limit = budget_exhaustion(self.store.budget_usage(session, db), self.limits,
                                                    self.store.clock(), resource="lifecycle")
                if lifecycle_limit and not checkpoint.get("result", {}).get("success"):
                    transition(session, S.EXHAUSTED)
                    session.last_error_category = lifecycle_limit
                    session.completed_at = self.store.clock()
                    changed = True
                elif any(r.status == "pending" for r in self._requests(db, session_id)):
                    transition(session, S.WAITING_HUMAN)
                    changed = True
                else:
                    session.lease_until = self.store.clock() + timedelta(seconds=self.limits.lease_seconds)
                    changed = True
            if changed:
                if session.status != S.RUNNING:
                    session.owner_token = None
                    session.lease_until = None
                if rows or context_changed or session.status != S.RUNNING:
                    self.store.checkpoint(session, checkpoint, db, self.limits.checkpoints_to_keep)
                self.store.save(session, db)
            return session, context_changed

    def _verify_reopen(self, session):
        """External verification precedes the short SQLite write transaction."""
        incident = self.incident_store.get_incident(session.incident_id, include_history=False)
        if incident is None or incident.status == "resolved" or incident.alert_state != "firing":
            return "incident_not_active"
        if self.alert_verifier is None:
            return "live_alert_verifier_required"
        import requests
        try:
            alert = self.alert_verifier(incident)
        except (requests.RequestException, OSError, ValueError) as exc:
            return "live_alert_verification_failed:" + type(exc).__name__
        from fingerprint import generate_alert_fingerprint
        if not self._fresh_alert(alert):
            return "live_firing_alert_not_confirmed"
        if alert.service != session.service or generate_alert_fingerprint(alert) != incident.fingerprint:
            return "live_alert_identity_mismatch"
        return alert

    def _reopen(self, previous, command, verified, db):
        if previous.status == S.COMPLETED:
            if not budget_exhaustion(self.store.budget_usage(previous, db), self.limits,
                                     self.store.clock(), new_attempt=True):
                raise RuntimeErrorBase("completed_reopen_requires_exhausted_incident_budget")
        elif previous.status not in {S.FAILED, S.EXHAUSTED, S.CANCELLED}:
            raise RuntimeErrorBase("reopen_requires_failed_exhausted_or_cancelled")
        latest = self.store.latest_session(previous.incident_id, db)
        if latest.session_id != previous.session_id:
            raise RuntimeErrorBase("reopen_requires_latest_session")
        if not self.store.load_checkpoint(previous, db).get("projected"):
            raise RuntimeErrorBase("previous_projection_pending")
        if isinstance(verified, str) or verified is None:
            raise RuntimeErrorBase(verified or "live_alert_verification_required")
        if not self._fresh_alert(verified):
            raise RuntimeErrorBase("live_alert_verification_expired")
        incident = self.incident_store.get_incident(previous.incident_id, include_history=False)
        if incident is None or incident.status == "resolved" or incident.alert_state != "firing":
            raise RuntimeErrorBase("incident_not_active")
        session = self._insert_session(incident, previous, db, command)
        checkpoint = self.store.load_checkpoint(session, db)
        checkpoint["reopen_alert"] = verified.model_dump(mode="json")
        self.store.checkpoint(session, checkpoint, db, self.limits.checkpoints_to_keep)
        self.store.save(session, db)
        usage = self.store.budget_usage(previous, db)
        usage["started_at"] = usage["started_at"].isoformat()
        self.store.event(db, previous.session_id, "investigation_reopened", command.actor,
                         command_id=command.command_id, reason=command.payload["reason"],
                         new_session_id=session.session_id, previous_status=previous.status.value,
                         previous_error=previous.last_error_category,
                         previous_usage=usage,
                         authorized_limits=self.limits.model_dump(),
                         alert_observed_at=verified.observed_at.isoformat())
        self.store.event(db, session.session_id, "budget_reauthorized", command.actor,
                         command_id=command.command_id, reason=command.payload["reason"],
                         previous_session_id=previous.session_id, authorized_limits=self.limits.model_dump(),
                         verified_alert=verified.model_dump(mode="json"),
                         alert_fingerprint=previous.alert_fingerprint)
        self.store.metadata(db, "reinvestigation_refusal:" + incident.incident_id, None, write=True)
        return session

    def _resume(self, session, checkpoint, db):
        saved_final = checkpoint.get("result", {}).get("success")
        usage = self.store.budget_usage(session, db)
        if (
            (exhausted(session, self.limits, self.store.clock()) and not saved_final)
            or session.resume_count >= self.limits.max_resumes
            or usage["resumes"] >= self.limits.max_resumes
            or (budget_exhaustion(usage, self.limits, self.store.clock()) and not saved_final)
        ):
            raise RuntimeErrorBase("session_budget_exhausted")
        session.resume_count += 1
        transition(session, S.QUEUED)
        session.queued_at = self.store.clock()
        session.next_retry_at = None

    def request_human(self, session_id, claim, question, checkpoint):
        with self.store.transaction() as db:
            session = self.store.guard(db, session_id, *claim)
            if self.store.budget_usage(session, db)["human_requests"] >= self.limits.max_human_requests:
                raise RuntimeErrorBase("human_request_budget_exhausted")
            request = HumanRequest(
                session_id=session_id,
                question=protect(question),
                created_at=self.store.clock(),
                expires_at=self.store.clock() + timedelta(seconds=self.limits.human_timeout_seconds),
            )
            db.execute("INSERT INTO human_requests VALUES (?,?,?,?)",
                       (request.request_id, session_id, "pending", request.model_dump_json()))
            session.human_request_count += 1
            transition(session, S.WAITING_HUMAN)
            session.owner_token = None
            session.lease_until = None
            self.store.checkpoint(session, checkpoint, db, self.limits.checkpoints_to_keep)
            self.store.save(session, db)
            self.store.event(db, session_id, "human_request_created", request_id=request.request_id)
            return request

    def recover(self):
        """Reconcile leases/expiry, then replay only durable JSON projections."""
        active = set(S) - TERMINAL
        for session in self.store.list_sessions(statuses=active):
            self._recover_session(session.session_id)
        for session_id in self.store.pending_commands():
            self.process_commands(session_id)
        for session in self.store.pending_work("projection"):
            self.sync_incident(session.session_id)
        for incident in self.incident_store.active_incidents(include_history=False):
            with self.store.read() as db:
                known = self.store.latest_session(incident.incident_id, db)
            if known is None:
                self.enqueue(incident)

    def _recover_session(self, session_id):
        incident = self.incident_store.get_incident(
            self.store.get(session_id).incident_id, include_history=False,
        )
        now = self.store.clock()
        with self.store.read() as db:
            session = self.store.get(session_id, db)
            if session.status in TERMINAL:
                return
            requests = self._requests(db, session_id)
            usage = self.store.budget_usage(session, db)
            live = session.status == S.RUNNING and session.lease_until and session.lease_until > now
            inactive = incident is None or incident.alert_state == "cleared" or incident.status == "resolved"
            saved_final = self.store.load_checkpoint(session, db).get("result", {}).get("success")
            budget = budget_exhaustion(usage, self.limits, now)
            retry_due = session.status == S.WAITING_RETRY and session.next_retry_at and session.next_retry_at <= now
            if not any(r.status == "pending" and r.expires_at <= now for r in requests) and (
                live or not (inactive or (budget and not saved_final) or session.status == S.RUNNING or retry_due)
            ):
                return
        with self.store.transaction() as db:
            now = self.store.clock()
            session = self.store.get(session_id, db)
            if session.status in TERMINAL:
                return
            checkpoint = self.store.load_checkpoint(session, db)
            changed = False
            for request in self._requests(db, session_id):
                if request.status == "pending" and request.expires_at <= now:
                    request.status = "expired"
                    self._put_request(db, request)
                    if session.status == S.WAITING_HUMAN:
                        transition(session, S.PAUSED)
                        session.last_error_category = "human_request_expired"
                        changed = True
                    self.store.event(db, session_id, "human_request_expired", request_id=request.request_id)
            live = session.status == S.RUNNING and session.lease_until and session.lease_until > now
            if not live:
                usage = self.store.budget_usage(session, db)
                reason = budget_exhaustion(usage, self.limits, now)
                if inactive:
                    transition(session, S.CANCELLED)
                    session.last_error_category = "incident_missing_or_alert_cleared"
                    self.store.event(db, session_id, "recovery_not_required")
                    changed = True
                elif reason and not checkpoint.get("result", {}).get("success"):
                    transition(session, S.EXHAUSTED)
                    session.last_error_category = reason
                    changed = True
                elif session.status == S.RUNNING:
                    if usage["resumes"] >= self.limits.max_resumes:
                        transition(session, S.EXHAUSTED)
                        session.last_error_category = "session_resume_budget_exhausted"
                    else:
                        session.resume_count += 1
                        transition(session, S.QUEUED)
                        session.queued_at = now
                    session.fencing_token += 1
                    self.store.event(db, session_id, "worker_recovered", unfinished_phase=checkpoint.get("phase"))
                    changed = True
                elif session.status == S.WAITING_RETRY and session.next_retry_at and session.next_retry_at <= now:
                    transition(session, S.QUEUED)
                    session.queued_at = now
                    changed = True
            if changed:
                session.owner_token = None
                session.lease_until = None
                if session.status in TERMINAL:
                    session.completed_at = now
                    for request in self._requests(db, session_id):
                        if request.status in {"pending", "answered"}:
                            request.status = "cancelled"
                            self._put_request(db, request)
                            self.store.event(db, session_id, "human_request_cancelled",
                                             request_id=request.request_id, reason=session.status.value)
                self.store.checkpoint(session, checkpoint, db, self.limits.checkpoints_to_keep)
                self.store.save(session, db)

    def sync_incident(self, session_id, claim=None):
        # SQLite -> JSON -> SQLite acknowledgement is an idempotent outbox, not a cross-store transaction.
        with self.store.read() as db:
            session = self.store.get(session_id, db)
            checkpoint = self.store.load_checkpoint(session, db)
        if checkpoint.get("projection_blocked"):
            return
        if checkpoint.get("projected") and (
            not checkpoint.get("remediation_pending") or checkpoint.get("remediation_blocked")
        ):
            return
        for field in ("projection_retry_at", "remediation_retry_at"):
            retry_at = checkpoint.get(field)
            if retry_at and datetime.fromisoformat(retry_at.replace("Z", "+00:00")) > self.store.clock():
                return
        events = []
        if not checkpoint.get("projected"):
            try:
                # Worker publication is serialized against fencing replacement.
                # JSON commit and outbox ack remain separate crash boundaries;
                # no model/tool I/O or remediation action is in this transaction.
                publication = self.store.transaction() if claim else nullcontext(None)
                with publication as db:
                    if claim:
                        current = self.store.get(session_id, db)
                        if current.fencing_token != claim[1]:
                            raise LostClaim("worker_claim_lost")
                        if current.status == S.RUNNING:
                            self.store.guard(db, session_id, *claim)
                        if current.state_version != session.state_version:
                            self.store.event(db, session_id, "incident_projection_superseded")
                            return
                    outcome = self.incident_store.apply_runtime_projection(session, checkpoint)
                if not isinstance(outcome, RuntimeProjectionResult):
                    raise RuntimeIntegrityError("unconfirmed_incident_projection")
                if outcome == RuntimeProjectionResult.MISSING:
                    checkpoint["projection_blocked"] = True
                    checkpoint["projection_error_category"] = "incident_not_found"
                    checkpoint["projection_retry_at"] = None
                    checkpoint["projection_attempts"] = checkpoint.get("projection_attempts", 0) + 1
                    events.append(("incident_projection_blocked", {"category": "incident_not_found"}))
                elif outcome not in {RuntimeProjectionResult.APPLIED, RuntimeProjectionResult.UNCHANGED,
                                      RuntimeProjectionResult.ARCHIVED}:
                    raise RuntimeIntegrityError("unconfirmed_incident_projection")
            except (OSError, FileLockError) as exc:
                attempts = checkpoint.get("projection_attempts", 0) + 1
                checkpoint["projection_attempts"] = attempts
                checkpoint["projection_error_category"] = type(exc).__name__
                delay = min(300, self.limits.retry_base_seconds * 2 ** min(attempts - 1, 8))
                checkpoint["projection_retry_at"] = (self.store.clock() + timedelta(seconds=delay)).isoformat()
                events.append(("incident_projection_failed", {"category": type(exc).__name__, "retry_seconds": delay}))
            else:
                if not checkpoint.get("projection_blocked"):
                    checkpoint["projection_retry_at"] = None
                    checkpoint["projection_error_category"] = None
        if not checkpoint.get("projection_error_category") and session.status in TERMINAL:
            was_pending = checkpoint.get("remediation_pending", False)
            checkpoint["remediation_pending"] = False
            checkpoint["remediation_retry_at"] = None
            if session.status == S.COMPLETED and self.remediation_planner is not None and not checkpoint.get("proposal_evidence_gap"):
                from investigation.evidence_policy import effective_records
                from models.incident import Evidence
                from models.investigation import InvestigationResult
                records = effective_records(checkpoint.get("records", []), checkpoint.get("identity"),
                                            self.store.clock(), session.service)
                versions = [r for r in records if r["name"] == "get_running_version"]
                if was_pending and not versions:
                    # Backoff cannot extend evidence TTL or silently erase a failed proposal.
                    checkpoint["remediation_pending"] = True
                    checkpoint["remediation_blocked"] = True
                    checkpoint["remediation_blocked_reason"] = "evidence_expired"
                    events.append(("remediation_attention_required", {"reason": "evidence_expired"}))
                elif versions and checkpoint.get("result", {}).get("success"):
                    from remediation.state import RemediationError
                    attempts = checkpoint.get("remediation_attempts", 0) + 1
                    checkpoint["remediation_attempts"] = attempts
                    try:
                        proposal = self.remediation_planner.propose(
                            self.incident_store.get_incident(session.incident_id),
                            InvestigationResult.model_validate(checkpoint["result"]),
                            [Evidence(source=r["name"], summary="Runtime 有效观测",
                                      detail=json.dumps(r["result"], ensure_ascii=False)) for r in records],
                        )
                        if proposal is not None:
                            events.append(("remediation_proposed", {"proposal_id": proposal.proposal_id, "status": proposal.status.value}))
                    except (RemediationError, OSError, FileLockError, ValueError) as exc:
                        category, retryable = _remediation_failure(exc)
                        checkpoint["remediation_pending"] = True
                        checkpoint["remediation_error_category"] = category
                        blocked = not retryable or attempts >= REMEDIATION_MAX_ATTEMPTS
                        checkpoint["remediation_blocked"] = blocked
                        checkpoint["remediation_blocked_reason"] = None
                        if not retryable:
                            checkpoint["remediation_blocked_reason"] = "non_retryable_error"
                        elif blocked:
                            checkpoint["remediation_blocked_reason"] = "attempt_limit"
                        delay = min(REMEDIATION_RETRY_MAX_SECONDS,
                                    self.limits.retry_base_seconds * 2 ** min(attempts - 1, 8))
                        if not blocked:
                            checkpoint["remediation_retry_at"] = (self.store.clock() + timedelta(seconds=delay)).isoformat()
                        events.append(("remediation_projection_failed", {
                            "category": category, "attempts": attempts,
                            "retry_seconds": None if blocked else delay, "blocked": blocked,
                        }))
                        if blocked or attempts == 3:
                            events.append(("remediation_attention_required", {
                                "category": category, "attempts": attempts,
                                "reason": checkpoint["remediation_blocked_reason"] or "persistent_failure",
                            }))
                    else:
                        if was_pending and proposal is None:
                            checkpoint["remediation_pending"] = True
                            checkpoint["remediation_blocked"] = True
                            checkpoint["remediation_blocked_reason"] = "evidence_no_longer_eligible"
                            events.append(("remediation_attention_required", {"reason": "evidence_no_longer_eligible"}))
                        else:
                            checkpoint["remediation_error_category"] = None
                            checkpoint["remediation_blocked"] = False
                            checkpoint["remediation_blocked_reason"] = None
                            if was_pending:
                                events.append(("remediation_projection_recovered", {"proposal_created": True}))
        if not checkpoint.get("projection_error_category"):
            checkpoint["projected"] = True
        # A concurrent Worker may have saved a newer checkpoint during file I/O.
        # Acknowledge only the snapshot actually projected; newer work stays pending.
        with self.store.transaction() as db:
            latest = self.store.get(session_id, db)
            if latest.state_version != session.state_version:
                self.store.event(db, session_id, "incident_projection_superseded")
                return
            self.store.checkpoint(session, checkpoint, db, self.limits.checkpoints_to_keep, acknowledge=True)
            self.store.save(session, db)
            for name, metadata in events:
                self.store.event(db, session_id, name, **metadata)
                if name in {"incident_projection_blocked", "remediation_projection_failed",
                            "remediation_attention_required", "remediation_projection_recovered"}:
                    self.store.metadata(db, "pending_notification:" + session_id, True, write=True)

    def workflow(self, session_id, include_remediations=True):
        with self.store.read() as db:
            session = self.store.get(session_id, db)
            checkpoint = self.store.load_checkpoint(session, db)
            requests = self._requests(db, session_id)
            messages = db.execute(
                "SELECT data,consumed_at FROM human_messages WHERE session_id=? ORDER BY sequence", (session_id,)
            ).fetchall()
            usage = self.store.budget_usage(session, db)
            usage["started_at"] = usage["started_at"].isoformat()
            refusal = self.store.metadata(db, "reinvestigation_refusal:" + session.incident_id)
        proposals = []
        remediation_store_error = None
        if self.remediation_planner is not None and include_remediations:
            from remediation.state import RemediationError
            try:
                proposals = [p.model_dump(mode="json") for p in self.remediation_planner.store.list()
                             if p.incident_id == session.incident_id]
            except (RemediationError, OSError, FileLockError, ValueError) as exc:
                remediation_store_error = _remediation_failure(exc)[0]
        remediation_status = ("blocked" if checkpoint.get("remediation_blocked") else
                              "waiting_retry" if checkpoint.get("remediation_pending") else "not_pending")
        return {
            "session": session.model_dump(mode="json"),
            "investigation_result": checkpoint.get("result"),
            "limits": self.limits.model_dump(mode="json"),
            "incident_budget_usage": usage,
            "reinvestigation_refusal": refusal,
            "proposal_evidence_gap": checkpoint.get("proposal_evidence_gap"),
            "projection_error_category": checkpoint.get("projection_error_category"),
            "projection_retry_at": checkpoint.get("projection_retry_at"),
            "projection_blocked": checkpoint.get("projection_blocked", False),
            "remediation_status": remediation_status,
            "remediation_pending": checkpoint.get("remediation_pending", False),
            "remediation_attempts": checkpoint.get("remediation_attempts", 0),
            "remediation_error_category": checkpoint.get("remediation_error_category"),
            "remediation_retry_at": checkpoint.get("remediation_retry_at"),
            "remediation_blocked": checkpoint.get("remediation_blocked", False),
            "remediation_blocked_reason": checkpoint.get("remediation_blocked_reason"),
            "remediation_store_error_category": remediation_store_error,
            "human_requests": [r.model_dump(mode="json") for r in requests],
            "human_messages": [json.loads(r["data"]) | {"consumed_at": r["consumed_at"]} for r in messages],
            "checkpoint_summary": {
                "phase": checkpoint["phase"],
                "budget": checkpoint.get("budget", {}),
                "pending_readonly_calls": len(checkpoint.get("pending", [])),
                "evidence_refs": [
                    {
                        "observation_id": r["observation_id"],
                        "source": r["name"],
                        "collected_at": r["collected_at"],
                        "status": r["result"].get("status"),
                    }
                    for r in checkpoint["records"]
                ],
            },
            "remediations": proposals,
            "approval_authority": "existing_remediation_store",
        }
