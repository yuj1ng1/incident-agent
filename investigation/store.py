"""MySQL Runtime store: short transactions, row locks and fenced claims."""
from contextlib import contextmanager
from datetime import timedelta, timezone
import json
from uuid import uuid4

from sqlalchemy import case, delete, distinct, func, inspect, select, text, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError, OperationalError

from db import tables
from db.config import database_url as configured_database_url, get_engine
from investigation.checkpoint import encode, projection_digest
from investigation.models import (
    Checkpoint,
    HumanCommand,
    HumanRequest,
    InvestigationSession,
    SessionStatus as S,
    utc_now,
)
from investigation.state import (
    LostClaim,
    RuntimeErrorBase,
    RuntimeIntegrityError,
    StateVersionConflict,
    TERMINAL,
    transition,
)


def _load_document(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


def _utc_naive(value):
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _utc_iso(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat()


class RuntimeStore:
    def __init__(self, database_url=None, clock=utc_now, *, read_only=False):
        self.database_url = database_url or configured_database_url()
        self.engine = get_engine(self.database_url)
        self.clock = clock
        self.read_only = read_only
        try:
            if not inspect(self.engine).has_table("investigation_sessions"):
                raise RuntimeIntegrityError("runtime_schema_not_migrated")
        except OperationalError:
            raise RuntimeIntegrityError("runtime_database_unavailable") from None

    @contextmanager
    def transaction(self):
        if self.read_only:
            raise RuntimeErrorBase("runtime_store_read_only")
        with self.engine.begin() as db:
            yield db

    @contextmanager
    def read(self):
        """Repeatable-read snapshot; the session default forbids writes."""
        connection = self.engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        )
        try:
            connection.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
            connection.commit()
            with connection.begin():
                yield connection
        finally:
            if connection.in_transaction():
                connection.rollback()
            try:
                connection.exec_driver_sql("SET SESSION TRANSACTION READ WRITE")
                connection.commit()
            finally:
                connection.close()

    @staticmethod
    def _session(row):
        session = InvestigationSession.model_validate(_load_document(row["data"]))
        if (
            session.session_id != row["session_id"]
            or session.state_version != row["state_version"]
            or session.status.value != row["status"]
        ):
            raise RuntimeIntegrityError("runtime_projection_mismatch")
        return session

    def _sessions(self, rows):
        return [self._session(row) for row in rows]

    def _add_work(self, db, kind, session_id):
        db.execute(
            mysql_insert(tables.runtime_work)
            .values(
                kind=kind,
                session_id=session_id,
                created_at=_utc_naive(self.clock()),
            )
            .prefix_with("IGNORE")
        )

    def add_work(self, db, kind, session_id):
        self._add_work(db, kind, session_id)

    def _remove_work(self, db, kind, session_id):
        db.execute(
            delete(tables.runtime_work).where(
                tables.runtime_work.c.kind == kind,
                tables.runtime_work.c.session_id == session_id,
            )
        )

    def get(self, session_id, db=None, *, for_update=False):
        if db is None:
            if for_update:
                raise RuntimeErrorBase("runtime_transaction_required")
            with self.read() as connection:
                return self.get(session_id, connection)
        statement = select(tables.investigation_sessions).where(
            tables.investigation_sessions.c.session_id == session_id
        )
        if for_update:
            statement = statement.with_for_update()
        row = db.execute(statement).mappings().first()
        if row is None:
            raise RuntimeIntegrityError("session_not_found")
        return self._session(row)

    def list_sessions(
        self,
        db=None,
        *,
        statuses=None,
        incident_id=None,
        limit=None,
        offset=0,
        newest_first=False,
    ):
        if offset < 0 or (limit is not None and not 1 <= limit <= 1000) or (offset and limit is None):
            raise RuntimeErrorBase("session_pagination_invalid")
        if db is None:
            with self.read() as connection:
                return self.list_sessions(
                    connection,
                    statuses=statuses,
                    incident_id=incident_id,
                    limit=limit,
                    offset=offset,
                    newest_first=newest_first,
                )
        statement = select(tables.investigation_sessions)
        if statuses is not None:
            if not statuses:
                return []
            statement = statement.where(
                tables.investigation_sessions.c.status.in_(
                    [S(status).value for status in statuses]
                )
            )
        if incident_id is not None:
            statement = statement.where(
                tables.investigation_sessions.c.incident_id == incident_id
            )
        order = (
            tables.investigation_sessions.c.created_seq.desc()
            if newest_first
            else tables.investigation_sessions.c.created_seq
        )
        statement = statement.order_by(order)
        if limit is not None:
            statement = statement.limit(limit).offset(offset)
        return self._sessions(db.execute(statement).mappings())

    def latest_session(self, incident_id, db):
        statement = (
            select(tables.investigation_sessions)
            .where(tables.investigation_sessions.c.incident_id == incident_id)
            .order_by(tables.investigation_sessions.c.created_seq.desc())
            .limit(1)
        )
        row = db.execute(statement).mappings().first()
        return self._session(row) if row is not None else None

    def budget_usage(self, session, db):
        statement = select(tables.investigation_sessions).where(
            tables.investigation_sessions.c.incident_id == session.incident_id
        )
        if session.budget_group_id is None:
            statement = statement.where(
                tables.investigation_sessions.c.budget_group_id.is_(None)
            )
        else:
            statement = statement.where(
                tables.investigation_sessions.c.budget_group_id
                == session.budget_group_id
            )
        group = self._sessions(db.execute(statement).mappings())
        return {
            "attempts": sum(item.attempt_count for item in group),
            "model_requests": sum(item.total_model_requests for item in group),
            "tool_executions": sum(item.total_tool_executions for item in group),
            "resumes": sum(item.resume_count for item in group),
            "human_requests": sum(item.human_request_count for item in group),
            "started_at": min((item.created_at for item in group), default=session.created_at),
        }

    def pending_commands(self):
        with self.read() as db:
            rows = db.execute(
                select(distinct(tables.investigation_commands.c.session_id)).where(
                    tables.investigation_commands.c.status == "pending"
                )
            )
            return [row[0] for row in rows]

    def pending_work(self, kind):
        if kind not in {"projection", "notification"}:
            raise RuntimeErrorBase("unknown_pending_work")
        with self.read() as db:
            statement = (
                select(tables.investigation_sessions)
                .join(
                    tables.runtime_work,
                    tables.runtime_work.c.session_id
                    == tables.investigation_sessions.c.session_id,
                )
                .where(tables.runtime_work.c.kind == kind)
                .order_by(tables.investigation_sessions.c.created_seq)
            )
            return self._sessions(db.execute(statement).mappings())

    def acknowledge_work(self, db, kind, session_id):
        self._remove_work(db, kind, session_id)

    def insert(self, session, db):
        values = {
            "session_id": session.session_id,
            "incident_id": session.incident_id,
            "status": session.status.value,
            "state_version": session.state_version,
            "budget_group_id": session.budget_group_id,
            "priority": session.priority,
            "queued_at": _utc_naive(session.queued_at),
            "lease_until": _utc_naive(session.lease_until),
            "fencing_token": session.fencing_token,
            "data": session.model_dump_json(),
        }
        try:
            db.execute(tables.investigation_sessions.insert().values(**values))
        except IntegrityError as exc:
            if getattr(exc.orig, "args", [None])[0] == 1062:
                raise RuntimeIntegrityError("session_insert_conflict") from None
            raise
        self.event(db, session.session_id, "session_created", "manager")
        self._add_work(db, "projection", session.session_id)

    def save(self, session, db):
        previous = self.get(session.session_id, db, for_update=True)
        previous_version = session.state_version
        previous_status = previous.status
        previous_error = previous.last_error_category
        session.updated_at = self.clock()
        session.state_version += 1
        validated = InvestigationSession.model_validate(session.model_dump())
        changed = db.execute(
            update(tables.investigation_sessions)
            .where(
                tables.investigation_sessions.c.session_id == validated.session_id,
                tables.investigation_sessions.c.state_version == previous_version,
            )
            .values(
                status=validated.status.value,
                state_version=validated.state_version,
                budget_group_id=validated.budget_group_id,
                priority=validated.priority,
                queued_at=_utc_naive(validated.queued_at),
                lease_until=_utc_naive(validated.lease_until),
                fencing_token=validated.fencing_token,
                data=validated.model_dump_json(),
            )
        ).rowcount
        if changed != 1:
            raise StateVersionConflict("session_version_conflict")
        visible = TERMINAL | {S.WAITING_HUMAN, S.PAUSED}
        if validated.status in visible and (
            previous_status != validated.status
            or previous_error != validated.last_error_category
        ):
            self._add_work(db, "notification", validated.session_id)
        elif validated.status not in visible:
            self._remove_work(db, "notification", validated.session_id)

    def guard(self, db, session_id, owner, fence, *, session=None):
        session = session or self.get(session_id, db, for_update=True)
        if (
            session.status != S.RUNNING
            or session.owner_token != owner
            or session.fencing_token != fence
            or session.lease_until is None
            or session.lease_until <= self.clock()
        ):
            raise LostClaim("worker_claim_lost")
        return session

    def event(self, db, session_id, event_type, actor="runtime", **metadata):
        db.execute(
            tables.investigation_events.insert().values(
                event_id="evt-" + uuid4().hex,
                session_id=session_id,
                event_type=event_type[:80],
                timestamp=_utc_naive(self.clock()),
                actor=str(actor),
                data=encode(metadata),
            )
        )

    def events(self, session_id, limit=100):
        if not 1 <= limit <= 1000:
            raise RuntimeErrorBase("event_limit_invalid")
        with self.read() as db:
            self.get(session_id, db)
            rows = db.execute(
                select(tables.investigation_events)
                .where(tables.investigation_events.c.session_id == session_id)
                .order_by(tables.investigation_events.c.sequence.desc())
                .limit(limit)
            ).mappings()
            return [
                {
                    "sequence": row["sequence"],
                    "event_id": row["event_id"],
                    "session_id": row["session_id"],
                    "event_type": row["event_type"],
                    "timestamp": _utc_iso(row["timestamp"]),
                    "actor": row["actor"],
                    "data": _load_document(row["data"]),
                }
                for row in reversed(list(rows))
            ]

    def checkpoint(self, session, payload, db, keep=4, *, acknowledge=False):
        Checkpoint.model_validate(payload)
        if (
            session.status in {S.COMPLETED, S.FAILED, S.EXHAUSTED}
            and payload.get("result")
            and self.get(session.session_id, db).status not in TERMINAL
        ):
            payload["result"].update(
                model_rounds=session.total_model_requests,
                tool_calls=session.total_tool_executions,
            )
        if not acknowledge:
            payload["projected"] = bool(payload.get("projected")) and (
                payload.get("projection_digest") == projection_digest(session, payload)
            )
        elif payload.get("projected"):
            payload["projection_digest"] = projection_digest(session, payload)
        checkpoint_id = "cp-" + uuid4().hex
        db.execute(
            tables.investigation_checkpoints.insert().values(
                checkpoint_id=checkpoint_id,
                session_id=session.session_id,
                created_at=_utc_naive(self.clock()),
                data=encode(payload),
            )
        )
        session.checkpoint_id = checkpoint_id
        self.event(
            db,
            session.session_id,
            "checkpoint_saved",
            phase=payload.get("phase"),
            checkpoint_id=checkpoint_id,
            observations=[record["observation_id"] for record in payload.get("records", [])],
        )
        if payload.get("projection_blocked"):
            self._add_work(db, "blocked_projection", session.session_id)
            self.acknowledge_work(db, "projection", session.session_id)
        elif payload.get("projected") and (
            not payload.get("remediation_pending") or payload.get("remediation_blocked")
        ):
            self._remove_work(db, "blocked_projection", session.session_id)
            self.acknowledge_work(db, "projection", session.session_id)
        else:
            self._add_work(db, "projection", session.session_id)
        db.execute(
            text(
                """DELETE FROM investigation_checkpoints
                    WHERE session_id=:session_id AND checkpoint_id NOT IN (
                        SELECT checkpoint_id FROM (
                            SELECT checkpoint_id FROM investigation_checkpoints
                            WHERE session_id=:session_id
                            ORDER BY sequence DESC LIMIT :keep
                        ) retained
                    )"""
            ),
            {"session_id": session.session_id, "keep": keep},
        )

    def load_checkpoint(self, session, db=None):
        if db is None:
            with self.read() as connection:
                return self.load_checkpoint(session, connection)
        if not session.checkpoint_id:
            return {
                "schema_version": 1,
                "phase": "ready",
                "records": [],
                "messages": [],
                "trace": [],
            }
        row = db.execute(
            select(
                tables.investigation_checkpoints.c.session_id,
                tables.investigation_checkpoints.c.data,
            ).where(
                tables.investigation_checkpoints.c.checkpoint_id
                == session.checkpoint_id
            )
        ).mappings().first()
        if row is None or row["session_id"] != session.session_id:
            raise RuntimeIntegrityError("checkpoint_missing_or_mismatched")
        payload = _load_document(row["data"])
        if payload.get("schema_version") != 1:
            raise RuntimeIntegrityError("unsupported_checkpoint_schema")
        Checkpoint.model_validate(payload)
        return payload

    def claim(self, owner, limits):
        # Idle ticks must stay read-only; the transaction below re-checks.
        with self.read() as db:
            queued = db.execute(
                select(tables.investigation_sessions.c.session_id)
                .where(tables.investigation_sessions.c.status == S.QUEUED.value)
                .limit(1)
            ).first()
            if queued is None:
                return None
        now = self.clock()
        with self.transaction() as db:
            db.execute(
                mysql_insert(tables.runtime_coordination)
                .values(name="worker_claim")
                .prefix_with("IGNORE")
            )
            db.execute(
                select(tables.runtime_coordination.c.name)
                .where(tables.runtime_coordination.c.name == "worker_claim")
                .with_for_update()
            ).first()
            running = db.execute(
                select(tables.investigation_sessions)
                .where(tables.investigation_sessions.c.status == S.RUNNING.value)
                .with_for_update()
            ).mappings()
            if any(
                row["lease_until"] is not None
                and row["lease_until"] > _utc_naive(now)
                for row in running
            ):
                return None
            queued_rows = db.execute(
                select(tables.investigation_sessions)
                .where(tables.investigation_sessions.c.status == S.QUEUED.value)
                .order_by(tables.investigation_sessions.c.created_seq)
                .with_for_update(skip_locked=True)
            ).mappings().all()
            candidates = self._sessions(queued_rows)
            if not candidates:
                return None
            candidates.sort(
                key=lambda session: (
                    0
                    if (now - session.queued_at).total_seconds()
                    >= limits.starvation_seconds
                    else 1,
                    -session.priority
                    if (now - session.queued_at).total_seconds()
                    < limits.starvation_seconds
                    else 0,
                    session.queued_at,
                    session.session_id,
                )
            )
            session = candidates[0]
            transition(session, S.RUNNING)
            session.owner_token = owner
            session.fencing_token += 1
            session.lease_until = now + timedelta(seconds=limits.lease_seconds)
            self.save(session, db)
            self.event(
                db,
                session.session_id,
                "worker_claimed",
                owner,
                fence=session.fencing_token,
            )
            return session

    def metadata(self, db, key, value=None, write=False):
        if len(key) > 191:
            raise RuntimeErrorBase("runtime_metadata_key_too_long")
        if write:
            statement = mysql_insert(tables.runtime_metadata).values(
                meta_key=key,
                meta_value=encode(value),
            )
            db.execute(
                statement.on_duplicate_key_update(
                    meta_value=statement.inserted.meta_value
                )
            )
            return value
        row = db.execute(
            select(tables.runtime_metadata.c.meta_value).where(
                tables.runtime_metadata.c.meta_key == key
            )
        ).first()
        return _load_document(row[0]) if row is not None else None

    def command(self, db, command_id, *, for_update=False):
        statement = select(tables.investigation_commands).where(
            tables.investigation_commands.c.command_id == command_id
        )
        if for_update:
            statement = statement.with_for_update()
        row = db.execute(statement).mappings().first()
        if row is None:
            return None
        return HumanCommand.model_validate(_load_document(row["data"]))

    def commands(
        self,
        db,
        session_id,
        *,
        status=None,
        for_update=False,
        cancel_first=False,
    ):
        statement = select(tables.investigation_commands).where(
            tables.investigation_commands.c.session_id == session_id
        )
        if status is not None:
            statement = statement.where(
                tables.investigation_commands.c.status == status
            )
        if cancel_first:
            statement = statement.order_by(
                case(
                    (tables.investigation_commands.c.command_type == "cancel", 0),
                    else_=1,
                ),
                tables.investigation_commands.c.sequence,
            )
        else:
            statement = statement.order_by(tables.investigation_commands.c.sequence)
        if for_update:
            statement = statement.with_for_update()
        return [
            HumanCommand.model_validate(_load_document(row["data"]))
            for row in db.execute(statement).mappings()
        ]

    def command_count(self, db, session_id):
        return db.execute(
            select(func.count())
            .select_from(tables.investigation_commands)
            .where(tables.investigation_commands.c.session_id == session_id)
        ).scalar_one()

    def command_text_bytes(self, db, session_id):
        rows = db.execute(
            select(tables.investigation_commands.c.data).where(
                tables.investigation_commands.c.session_id == session_id
            )
        ).scalars()
        total = 0
        for value in rows:
            value = _load_document(value)
            text_value = (value.get("payload") or {}).get("text")
            if isinstance(text_value, str):
                total += len(text_value.encode("utf-8"))
        return total

    def insert_command(self, db, command):
        db.execute(
            tables.investigation_commands.insert().values(
                command_id=command.command_id,
                session_id=command.session_id,
                status=command.processing_status,
                command_type=command.command_type,
                created_at=_utc_naive(self.clock()),
                data=command.model_dump_json(),
            )
        )

    def update_command(self, db, command):
        db.execute(
            update(tables.investigation_commands)
            .where(tables.investigation_commands.c.command_id == command.command_id)
            .values(
                status=command.processing_status,
                data=command.model_dump_json(),
            )
        )

    def requests(self, db=None, session_id=None, *, for_update=False):
        if db is None:
            with self.read() as connection:
                return self.requests(connection, session_id, for_update=for_update)
        statement = select(tables.human_requests)
        if session_id is not None:
            statement = statement.where(
                tables.human_requests.c.session_id == session_id
            )
        statement = statement.order_by(tables.human_requests.c.created_seq)
        if for_update:
            statement = statement.with_for_update()
        values = []
        for row in db.execute(statement).mappings():
            values.append(HumanRequest.model_validate(_load_document(row["data"])))
        return values

    def put_request(self, db, request):
        db.execute(
            update(tables.human_requests)
            .where(tables.human_requests.c.request_id == request.request_id)
            .values(
                status=request.status,
                data=request.model_dump_json(),
            )
        )

    def insert_request(self, db, request):
        try:
            db.execute(
                tables.human_requests.insert().values(
                    request_id=request.request_id,
                    session_id=request.session_id,
                    status=request.status,
                    expires_at=_utc_naive(request.expires_at),
                    data=request.model_dump_json(),
                )
            )
        except IntegrityError as exc:
            if getattr(exc.orig, "args", [None])[0] == 1062:
                raise RuntimeIntegrityError("human_request_conflict") from None
            raise

    def human_messages(self, db, session_id, *, unconsumed=False, for_update=False):
        statement = select(tables.human_messages).where(
            tables.human_messages.c.session_id == session_id
        )
        if unconsumed:
            statement = statement.where(
                tables.human_messages.c.consumed_at.is_(None)
            )
        statement = statement.order_by(tables.human_messages.c.sequence)
        if for_update:
            statement = statement.with_for_update()
        return [
            {
                "sequence": row["sequence"],
                "command_id": row["command_id"],
                "session_id": row["session_id"],
                "consumed_at": _utc_iso(row["consumed_at"]),
                "data": _load_document(row["data"]),
            }
            for row in db.execute(statement).mappings()
        ]

    def insert_human_message(self, db, command_id, session_id, payload):
        db.execute(
            tables.human_messages.insert().values(
                command_id=command_id,
                session_id=session_id,
                data=encode(payload),
            )
        )

    def consume_message(self, db, command_id):
        db.execute(
            update(tables.human_messages)
            .where(tables.human_messages.c.command_id == command_id)
            .values(consumed_at=_utc_naive(self.clock()))
        )
