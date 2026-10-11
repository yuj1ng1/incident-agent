"""Short SQLite IMMEDIATE transactions, CAS versions and expiring fenced claims."""
from contextlib import contextmanager
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from investigation.checkpoint import encode, projection_digest
from investigation.models import Checkpoint, InvestigationSession, SessionStatus as S, utc_now
from investigation.state import LostClaim, RuntimeErrorBase, RuntimeIntegrityError, StateVersionConflict, TERMINAL, transition

DEFAULT_STORE = Path(__file__).resolve().parents[1] / "data" / "runtime.sqlite3"
SCHEMA_VERSION = 1


class RuntimeStore:
    def __init__(self, path=DEFAULT_STORE, clock=utc_now, *, read_only=False):
        self.path = Path(path)
        self.clock = clock
        self.read_only = read_only
        if read_only:
            if not self.path.is_file():
                raise FileNotFoundError("runtime_store_not_found")
            with self.read() as db:
                if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                    raise RuntimeIntegrityError("unsupported_runtime_schema")
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeIntegrityError("runtime_database_corrupt")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A corrupt or newer DB fails closed; never replace it with an empty DB.
        with self.transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeIntegrityError("unsupported_runtime_schema")
            if version == 0:
                existing = db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                if existing:
                    raise RuntimeIntegrityError("unversioned_runtime_database")
                statements = [
                    """CREATE TABLE investigation_sessions (
                        session_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL,
                        status TEXT NOT NULL, state_version INTEGER NOT NULL,
                        data TEXT NOT NULL)""",
                    """CREATE UNIQUE INDEX one_active_incident ON investigation_sessions(incident_id)
                        WHERE status NOT IN ('COMPLETED','EXHAUSTED','FAILED','CANCELLED')""",
                    """CREATE TABLE investigation_checkpoints (
                        checkpoint_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES investigation_sessions(session_id),
                        created_at TEXT NOT NULL, data TEXT NOT NULL)""",
                    """CREATE TABLE investigation_commands (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        command_id TEXT NOT NULL UNIQUE,
                        session_id TEXT NOT NULL REFERENCES investigation_sessions(session_id),
                        status TEXT NOT NULL, data TEXT NOT NULL)""",
                    """CREATE TABLE human_messages (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        command_id TEXT NOT NULL UNIQUE REFERENCES investigation_commands(command_id),
                        session_id TEXT NOT NULL REFERENCES investigation_sessions(session_id),
                        consumed_at TEXT, data TEXT NOT NULL)""",
                    """CREATE TABLE human_requests (
                        request_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES investigation_sessions(session_id),
                        status TEXT NOT NULL, data TEXT NOT NULL)""",
                    """CREATE UNIQUE INDEX one_pending_request ON human_requests(session_id)
                        WHERE status='pending'""",
                    """CREATE TABLE investigation_events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL UNIQUE,
                        session_id TEXT NOT NULL REFERENCES investigation_sessions(session_id),
                        event_type TEXT NOT NULL, timestamp TEXT NOT NULL,
                        actor TEXT NOT NULL, data TEXT NOT NULL)""",
                    "CREATE TABLE runtime_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                    "PRAGMA user_version=1",
                ]
                for statement in statements:
                    db.execute(statement)
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeIntegrityError("runtime_database_corrupt")
            # Additive v1 indexes/outboxes: no table/column change or data reset.
            for statement in (
                "CREATE INDEX IF NOT EXISTS sessions_incident ON investigation_sessions(incident_id)",
                "CREATE INDEX IF NOT EXISTS sessions_budget_group ON investigation_sessions(incident_id,json_extract(data,'$.budget_group_id'))",
                "CREATE INDEX IF NOT EXISTS sessions_status ON investigation_sessions(status)",
                "CREATE INDEX IF NOT EXISTS commands_pending ON investigation_commands(status,session_id)",
                "CREATE INDEX IF NOT EXISTS requests_session ON human_requests(session_id,status)",
                "CREATE INDEX IF NOT EXISTS events_session ON investigation_events(session_id,sequence)",
            ):
                db.execute(statement)
            if not self.metadata(db, "work_outboxes_v1"):
                # Existing databases are backfilled once. Subsequent ticks query
                # indexed pending keys instead of opening historical checkpoints.
                for session in self.list_sessions(db):
                    checkpoint = self.load_checkpoint(session, db)
                    if checkpoint.get("projection_blocked"):
                        self.metadata(db, "blocked_projection:" + session.session_id, True, write=True)
                    elif not checkpoint.get("projected") or (
                        checkpoint.get("remediation_pending") and not checkpoint.get("remediation_blocked")
                    ):
                        self.metadata(db, "pending_projection:" + session.session_id, True, write=True)
                    if session.status in TERMINAL | {S.WAITING_HUMAN, S.PAUSED}:
                        self.metadata(db, "pending_notification:" + session.session_id, True, write=True)
                self.metadata(db, "work_outboxes_v1", True, write=True)

    @contextmanager
    def transaction(self):
        if self.read_only:
            raise RuntimeErrorBase("runtime_store_read_only")
        db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA synchronous=FULL")
            # DELETE journal intentionally avoids Windows WAL handling assumptions.
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def read(self):
        """Consistent read-only snapshot; never reserves the SQLite writer lock."""
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True,
                             timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            yield db
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _session(row):
        session = InvestigationSession.model_validate_json(row["data"])
        if session.state_version != row["state_version"] or session.status.value != row["status"]:
            raise RuntimeIntegrityError("runtime_projection_mismatch")
        return session

    def get(self, session_id, db=None):
        if db is None:
            with self.read() as connection:
                return self.get(session_id, connection)
        row = db.execute(
            "SELECT * FROM investigation_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise RuntimeIntegrityError("session_not_found")
        return self._session(row)

    def list_sessions(self, db=None, *, statuses=None, incident_id=None,
                      limit=None, offset=0, newest_first=False):
        if offset < 0 or (limit is not None and not 1 <= limit <= 1000) or (offset and limit is None):
            raise RuntimeErrorBase("session_pagination_invalid")
        if db is None:
            with self.read() as connection:
                return self.list_sessions(connection, statuses=statuses, incident_id=incident_id,
                                          limit=limit, offset=offset, newest_first=newest_first)
        conditions, values = [], []
        if statuses is not None:
            if not statuses:
                return []
            conditions.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            values.extend(S(status).value for status in statuses)
        if incident_id is not None:
            conditions.append("incident_id=?")
            values.append(incident_id)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        order = " ORDER BY rowid DESC" if newest_first else " ORDER BY rowid"
        page = " LIMIT ? OFFSET ?" if limit is not None else ""
        if limit is not None:
            values.extend((limit, offset))
        rows = db.execute("SELECT * FROM investigation_sessions" + where + order + page, values)
        return [self._session(row) for row in rows]

    def latest_session(self, incident_id, db):
        row = db.execute(
            "SELECT * FROM investigation_sessions WHERE incident_id=? ORDER BY rowid DESC LIMIT 1",
            (incident_id,),
        ).fetchone()
        return self._session(row) if row else None

    def budget_usage(self, session, db):
        group = [self._session(row) for row in db.execute(
            """SELECT * FROM investigation_sessions WHERE incident_id=?
                AND json_extract(data,'$.budget_group_id') IS ?""",
            (session.incident_id, session.budget_group_id),
        )]
        return {
            "attempts": sum(s.attempt_count for s in group),
            "model_requests": sum(s.total_model_requests for s in group),
            "tool_executions": sum(s.total_tool_executions for s in group),
            "resumes": sum(s.resume_count for s in group),
            "human_requests": sum(s.human_request_count for s in group),
            "started_at": min((s.created_at for s in group), default=session.created_at),
        }

    def pending_commands(self):
        with self.read() as db:
            return [row[0] for row in db.execute(
                "SELECT DISTINCT session_id FROM investigation_commands WHERE status='pending'"
            )]

    def pending_work(self, kind):
        if kind not in {"projection", "notification"}:
            raise RuntimeErrorBase("unknown_pending_work")
        prefix = "pending_" + kind + ":"
        with self.read() as db:
            rows = db.execute(
                """SELECT s.* FROM runtime_metadata m JOIN investigation_sessions s
                    ON s.session_id=substr(m.key,?) WHERE m.key>=? AND m.key<? ORDER BY s.rowid""",
                (len(prefix) + 1, prefix, prefix + "\uffff"),
            )
            return [self._session(row) for row in rows]

    def acknowledge_work(self, db, kind, session_id):
        db.execute("DELETE FROM runtime_metadata WHERE key=?", ("pending_" + kind + ":" + session_id,))

    def insert(self, session, db):
        db.execute(
            "INSERT INTO investigation_sessions VALUES (?,?,?,?,?)",
            (session.session_id, session.incident_id, session.status.value,
             session.state_version, session.model_dump_json()),
        )
        self.event(db, session.session_id, "session_created", "manager")
        self.metadata(db, "pending_projection:" + session.session_id, True, write=True)

    def save(self, session, db):
        previous = self.get(session.session_id, db)
        previous_version = session.state_version
        session.updated_at = self.clock()
        session.state_version += 1
        validated = InvestigationSession.model_validate(session.model_dump())
        changed = db.execute(
            """UPDATE investigation_sessions SET status=?,state_version=?,data=?
                WHERE session_id=? AND state_version=?""",
            (validated.status.value, validated.state_version, validated.model_dump_json(),
             validated.session_id, previous_version),
        ).rowcount
        if changed != 1:
            raise StateVersionConflict("session_version_conflict")
        visible = TERMINAL | {S.WAITING_HUMAN, S.PAUSED}
        if session.status in visible and (
            previous.status != session.status or previous.last_error_category != session.last_error_category
        ):
            self.metadata(db, "pending_notification:" + session.session_id, True, write=True)
        elif session.status not in visible:
            self.acknowledge_work(db, "notification", session.session_id)

    def guard(self, db, session_id, owner, fence):
        session = self.get(session_id, db)
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
            """INSERT INTO investigation_events
                (event_id,session_id,event_type,timestamp,actor,data) VALUES (?,?,?,?,?,?)""",
            ("evt-" + uuid4().hex, session_id, event_type,
             self.clock().isoformat(), actor, encode(metadata)),
        )

    def events(self, session_id, limit=100):
        if not 1 <= limit <= 1000:
            raise RuntimeErrorBase("event_limit_invalid")
        with self.read() as db:
            self.get(session_id, db)
            rows = db.execute(
                """SELECT * FROM investigation_events WHERE session_id=?
                    ORDER BY sequence DESC LIMIT ?""", (session_id, limit),
            ).fetchall()
            return [dict(row) | {"data": json.loads(row["data"])} for row in reversed(rows)]

    def checkpoint(self, session, payload, db, keep=4, *, acknowledge=False):
        Checkpoint.model_validate(payload)
        if not acknowledge:
            payload["projected"] = bool(payload.get("projected")) and (
                payload.get("projection_digest") == projection_digest(session, payload)
            )
        elif payload.get("projected"):
            payload["projection_digest"] = projection_digest(session, payload)
        checkpoint_id = "cp-" + uuid4().hex
        db.execute(
            "INSERT INTO investigation_checkpoints VALUES (?,?,?,?)",
            (checkpoint_id, session.session_id, self.clock().isoformat(), encode(payload)),
        )
        session.checkpoint_id = checkpoint_id
        self.event(db, session.session_id, "checkpoint_saved", phase=payload.get("phase"),
                   checkpoint_id=checkpoint_id, observations=[r["observation_id"] for r in payload.get("records", [])])
        if payload.get("projection_blocked"):
            self.metadata(db, "blocked_projection:" + session.session_id, True, write=True)
            self.acknowledge_work(db, "projection", session.session_id)
        elif payload.get("projected") and (
            not payload.get("remediation_pending") or payload.get("remediation_blocked")
        ):
            db.execute("DELETE FROM runtime_metadata WHERE key=?", ("blocked_projection:" + session.session_id,))
            self.acknowledge_work(db, "projection", session.session_id)
        else:
            self.metadata(db, "pending_projection:" + session.session_id, True, write=True)
        db.execute(
            """DELETE FROM investigation_checkpoints WHERE session_id=? AND checkpoint_id NOT IN (
                SELECT checkpoint_id FROM investigation_checkpoints WHERE session_id=?
                ORDER BY rowid DESC LIMIT ?)""",
            (session.session_id, session.session_id, keep),
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
            "SELECT session_id,data FROM investigation_checkpoints WHERE checkpoint_id=?",
            (session.checkpoint_id,),
        ).fetchone()
        if row is None or row[0] != session.session_id:
            raise RuntimeIntegrityError("checkpoint_missing_or_mismatched")
        payload = json.loads(row[1])
        if payload.get("schema_version") != 1:
            raise RuntimeIntegrityError("unsupported_checkpoint_schema")
        Checkpoint.model_validate(payload)
        return payload

    def claim(self, owner, limits):
        with self.read() as db:
            if not db.execute("SELECT 1 FROM investigation_sessions WHERE status='QUEUED' LIMIT 1").fetchone():
                return None
        with self.transaction() as db:
            sessions = self.list_sessions(db, statuses={S.RUNNING, S.QUEUED})
            now = self.clock()
            # Across all sessions there is at most one unexpired Worker claim.
            if any(s.status == S.RUNNING and s.lease_until and s.lease_until > now for s in sessions):
                return None
            candidates = [s for s in sessions if s.status == S.QUEUED]
            if not candidates:
                return None
            candidates.sort(key=lambda s: (
                0 if (now - s.queued_at).total_seconds() >= limits.starvation_seconds else 1,
                -s.priority if (now - s.queued_at).total_seconds() < limits.starvation_seconds else 0,
                s.queued_at,
                s.session_id,
            ))
            session = candidates[0]
            transition(session, S.RUNNING)
            session.owner_token = owner
            session.fencing_token += 1
            session.lease_until = now + timedelta(seconds=limits.lease_seconds)
            self.save(session, db)
            self.event(db, session.session_id, "worker_claimed", owner, fence=session.fencing_token)
            return session

    def metadata(self, db, key, value=None, write=False):
        if write:
            db.execute(
                "INSERT INTO runtime_metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, encode(value)),
            )
            return value
        row = db.execute("SELECT value FROM runtime_metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None
