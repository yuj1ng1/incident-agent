"""Application service shared by local CLI, Monitor and future authenticated Web."""
from datetime import timedelta
import json

from config import RUNTIME_LIMITS
from investigation.checkpoint import encode, protect
from investigation.models import HumanCommand, HumanRequest, InvestigationSession, RuntimeLimits, SessionStatus as S
from investigation.state import RuntimeErrorBase, TERMINAL, exhausted, transition


class InvestigationManager:
    def __init__(self, store, incident_store, remediation_planner=None, limits=None):
        self.store = store
        self.incident_store = incident_store
        self.remediation_planner = remediation_planner
        self.limits = limits or RuntimeLimits(**RUNTIME_LIMITS)

    def enqueue(self, incident, reinvestigate=False):
        with self.store.transaction() as db:
            previous = [s for s in self.store.list_sessions(db) if s.incident_id == incident.incident_id]
            if previous:
                latest = previous[-1]
                if latest.status != S.COMPLETED or not reinvestigate:
                    return latest
                checkpoint = self.store.load_checkpoint(latest, db)
                if not checkpoint.get("projected"):
                    return latest
            session = InvestigationSession(
                incident_id=incident.incident_id,
                alert_fingerprint=incident.fingerprint,
                service=incident.service,
                priority=1 if incident.severity.lower() == "critical" else 0,
                created_at=self.store.clock(),
                updated_at=self.store.clock(),
                queued_at=self.store.clock(),
            )
            self.store.insert(session, db)
            transition(session, S.QUEUED)
            self.store.save(session, db)
            self.store.event(db, session.session_id, "queued", "monitor")
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
        allowed = {"text"} if command.command_type == "message" else (
            {"text", "request_id"} if command.command_type == "answer" else set()
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
        return self.get_command(command.command_id)

    def get_command(self, command_id):
        with self.store.transaction() as db:
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
        with self.store.transaction() as db:
            sessions = [self.store.get(session_id, db)] if session_id else self.store.list_sessions(db)
            return [r for s in sessions for r in self._requests(db, s.session_id)]

    def process_commands(self, session_id, claim=None, payload=None):
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
                    if session.status in TERMINAL:
                        raise RuntimeErrorBase("session_terminal")
                    kind = command.command_type
                    if kind in {"message", "answer"}:
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
                                self._resume(session, checkpoint)
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
                        self._resume(session, checkpoint)
                        if session.last_error_category in {"model_http_401", "model_http_402", "model_http_403"}:
                            self.store.metadata(db, "model_blocked", None, write=True)
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
                    command.result = session.status.value
                    changed = True
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
                if (
                    (self.store.clock() - session.created_at).total_seconds() >= self.limits.max_duration_seconds
                    or session.resume_count > self.limits.max_resumes
                ) and not checkpoint.get("result", {}).get("success"):
                    transition(session, S.EXHAUSTED)
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

    def _resume(self, session, checkpoint):
        saved_final = checkpoint.get("result", {}).get("success")
        if (
            (exhausted(session, self.limits, self.store.clock()) and not saved_final)
            or session.resume_count >= self.limits.max_resumes
        ):
            raise RuntimeErrorBase("session_budget_exhausted")
        session.resume_count += 1
        transition(session, S.QUEUED)
        session.queued_at = self.store.clock()
        session.next_retry_at = None

    def request_human(self, session_id, claim, question, checkpoint):
        with self.store.transaction() as db:
            session = self.store.guard(db, session_id, *claim)
            if session.human_request_count >= self.limits.max_human_requests:
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
        with self.store.transaction() as db:
            now = self.store.clock()
            for session in self.store.list_sessions(db):
                changed = False
                for request in self._requests(db, session.session_id):
                    if request.status == "pending" and request.expires_at <= now:
                        request.status = "expired"
                        self._put_request(db, request)
                        if session.status == S.WAITING_HUMAN:
                            transition(session, S.PAUSED)
                            session.last_error_category = "human_request_expired"
                            changed = True
                        self.store.event(db, session.session_id, "human_request_expired",
                                         request_id=request.request_id)
                if session.status not in TERMINAL:
                    incident = self.incident_store.get_incident(session.incident_id)
                    live_claim = session.status == S.RUNNING and session.lease_until and session.lease_until > now
                    if not live_claim:
                        if incident is None or incident.alert_state == "cleared" or incident.status == "resolved":
                            transition(session, S.CANCELLED)
                            session.last_error_category = "incident_missing_or_alert_cleared"
                            self.store.event(db, session.session_id, "recovery_not_required")
                            changed = True
                        elif (
                            exhausted(session, self.limits, now)
                            and not self.store.load_checkpoint(session, db).get("result", {}).get("success")
                        ):
                            transition(session, S.EXHAUSTED)
                            changed = True
                        elif session.status == S.RUNNING:
                            if session.resume_count >= self.limits.max_resumes:
                                transition(session, S.EXHAUSTED)
                            else:
                                session.resume_count += 1
                                transition(session, S.QUEUED)
                                session.queued_at = now
                            session.fencing_token += 1
                            self.store.event(db, session.session_id, "worker_recovered",
                                             unfinished_phase=self.store.load_checkpoint(session, db).get("phase"))
                            changed = True
                        elif session.status == S.WAITING_RETRY and session.next_retry_at <= now:
                            transition(session, S.QUEUED)
                            session.queued_at = now
                            changed = True
                if changed:
                    session.owner_token = None
                    session.lease_until = None
                    if session.status in TERMINAL:
                        session.completed_at = now
                        for request in self._requests(db, session.session_id):
                            if request.status in {"pending", "answered"}:
                                request.status = "cancelled"
                                self._put_request(db, request)
                                self.store.event(db, session.session_id, "human_request_cancelled",
                                                 request_id=request.request_id, reason=session.status.value)
                    self.store.save(session, db)
        for session in self.store.list_sessions():
            self.process_commands(session.session_id)
            self.sync_incident(session.session_id)
        known_incidents = {s.incident_id for s in self.store.list_sessions()}
        from reinvestigation import should_reinvestigate
        for incident in self.incident_store.active_incidents():
            if (
                incident.incident_id not in known_incidents
                and incident.alerts
                and should_reinvestigate(incident, incident.alerts[-1])
            ):
                self.enqueue(incident)

    def sync_incident(self, session_id):
        # SQLite -> JSON -> SQLite acknowledgement is an idempotent outbox, not a cross-store transaction.
        with self.store.transaction() as db:
            session = self.store.get(session_id, db)
            checkpoint = self.store.load_checkpoint(session, db)
            if checkpoint.get("projected") and not checkpoint.get("remediation_pending"):
                return
            retry_at = checkpoint.get("remediation_retry_at")
            if retry_at:
                from datetime import datetime
                if datetime.fromisoformat(retry_at.replace("Z", "+00:00")) > self.store.clock():
                    return
            if not checkpoint.get("projected"):
                self.incident_store.apply_runtime_projection(session, checkpoint)
            if session.status in TERMINAL:
                checkpoint["remediation_pending"] = False
                if session.status == S.COMPLETED and self.remediation_planner is not None:
                    from investigation.evidence_policy import effective_records
                    from models.incident import Evidence
                    from models.investigation import InvestigationResult
                    records = effective_records(checkpoint.get("records", []), checkpoint.get("identity"), self.store.clock())
                    versions = [r for r in records if r["name"] == "get_running_version"]
                    if versions and checkpoint.get("result", {}).get("success"):
                        from remediation.state import RemediationError
                        try:
                            proposal = self.remediation_planner.propose(
                                self.incident_store.get_incident(session.incident_id),
                                InvestigationResult.model_validate(checkpoint["result"]),
                                [Evidence(source=r["name"], summary="Runtime 有效观测",
                                          detail=json.dumps(r["result"], ensure_ascii=False)) for r in records],
                            )
                            if proposal is not None:
                                self.store.event(db, session_id, "remediation_proposed",
                                                 proposal_id=proposal.proposal_id, status=proposal.status.value)
                        except (RemediationError, OSError, ValueError) as exc:
                            checkpoint["remediation_pending"] = True
                            checkpoint["remediation_retry_at"] = (self.store.clock() + timedelta(seconds=30)).isoformat()
                            self.store.event(db, session_id, "remediation_projection_failed", category=type(exc).__name__)
                checkpoint["projected"] = True
                self.store.checkpoint(session, checkpoint, db, self.limits.checkpoints_to_keep)
                self.store.save(session, db)

    def workflow(self, session_id):
        session = self.store.get(session_id)
        checkpoint = self.store.load_checkpoint(session)
        with self.store.transaction() as db:
            messages = db.execute(
                "SELECT data,consumed_at FROM human_messages WHERE session_id=? ORDER BY sequence", (session_id,)
            ).fetchall()
        proposals = [] if self.remediation_planner is None else [
            p.model_dump(mode="json") for p in self.remediation_planner.store.list()
            if p.incident_id == session.incident_id
        ]
        return {
            "session": session.model_dump(mode="json"),
            "human_requests": [r.model_dump(mode="json") for r in self.requests(session_id)],
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
