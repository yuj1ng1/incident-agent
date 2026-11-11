"""SQLAlchemy table metadata for the authoritative Runtime store.

Alembic owns production DDL; this module mirrors the same tables for typed
queries and for isolated test schemas.
"""
from sqlalchemy import (
    BigInteger,
    Column,
    Computed,
    ForeignKeyConstraint,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME, MEDIUMTEXT, TEXT, insert as mysql_insert

TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    # Binary, NO PAD comparisons preserve identifiers with trailing spaces.
    "mysql_collate": "utf8mb4_0900_bin",
}
UTC_DATETIME = DATETIME(fsp=6)
# MySQL JSON normalizes object key order. Documents stay as text so persisted
# results keep the exact structure and order the API produced.
DOCUMENT = MEDIUMTEXT()
VALUE = TEXT()
TERMINAL_SQL = "('COMPLETED','EXHAUSTED','FAILED','CANCELLED')"

metadata = MetaData()

investigation_sessions = Table(
    "investigation_sessions",
    metadata,
    Column("created_seq", BigInteger, primary_key=True, autoincrement=True),
    Column("session_id", String(120), nullable=False),
    Column("incident_id", String(120), nullable=False),
    Column("status", String(32), nullable=False),
    Column("state_version", BigInteger, nullable=False),
    Column("budget_group_id", String(120)),
    Column("priority", SmallInteger, nullable=False),
    Column("queued_at", UTC_DATETIME, nullable=False),
    Column("lease_until", UTC_DATETIME),
    Column("fencing_token", BigInteger, nullable=False),
    Column("data", DOCUMENT, nullable=False),
    Column(
        "active_incident_id",
        String(120),
        Computed(
            "CASE WHEN status IN " + TERMINAL_SQL + " "
            "THEN NULL ELSE incident_id END",
            persisted=True,
        ),
    ),
    PrimaryKeyConstraint("created_seq", name="pk_investigation_sessions"),
    UniqueConstraint("session_id", name="uq_investigation_sessions_session_id"),
    Index("one_active_incident", "active_incident_id", unique=True),
    Index(
        "ix_investigation_sessions_incident_created",
        "incident_id",
        "created_seq",
    ),
    Index(
        "ix_investigation_sessions_budget_group",
        "incident_id",
        "budget_group_id",
    ),
    Index(
        "ix_investigation_sessions_status_queued",
        "status",
        "queued_at",
    ),
    **TABLE_OPTIONS,
)

investigation_checkpoints = Table(
    "investigation_checkpoints",
    metadata,
    Column("sequence", BigInteger, primary_key=True, autoincrement=True),
    Column("checkpoint_id", String(120), nullable=False),
    Column("session_id", String(120), nullable=False),
    Column("created_at", UTC_DATETIME, nullable=False),
    Column("data", DOCUMENT, nullable=False),
    PrimaryKeyConstraint("sequence", name="pk_investigation_checkpoints"),
    UniqueConstraint(
        "checkpoint_id",
        name="uq_investigation_checkpoints_checkpoint_id",
    ),
    ForeignKeyConstraint(
        ["session_id"],
        ["investigation_sessions.session_id"],
        name="fk_investigation_checkpoints_session_id_investigation_sessions",
    ),
    Index(
        "ix_investigation_checkpoints_session_sequence",
        "session_id",
        "sequence",
    ),
    **TABLE_OPTIONS,
)

investigation_commands = Table(
    "investigation_commands",
    metadata,
    Column("sequence", BigInteger, primary_key=True, autoincrement=True),
    Column("command_id", String(120), nullable=False),
    Column("session_id", String(120), nullable=False),
    Column("status", String(32), nullable=False),
    Column("command_type", String(32), nullable=False),
    Column("created_at", UTC_DATETIME, nullable=False),
    Column("data", DOCUMENT, nullable=False),
    PrimaryKeyConstraint("sequence", name="pk_investigation_commands"),
    UniqueConstraint(
        "command_id",
        name="uq_investigation_commands_command_id",
    ),
    ForeignKeyConstraint(
        ["session_id"],
        ["investigation_sessions.session_id"],
        name="fk_investigation_commands_session_id_investigation_sessions",
    ),
    Index(
        "ix_investigation_commands_pending",
        "status",
        "session_id",
    ),
    Index(
        "ix_investigation_commands_session_sequence",
        "session_id",
        "sequence",
    ),
    **TABLE_OPTIONS,
)

human_messages = Table(
    "human_messages",
    metadata,
    Column("sequence", BigInteger, primary_key=True, autoincrement=True),
    Column("command_id", String(120), nullable=False),
    Column("session_id", String(120), nullable=False),
    Column("consumed_at", UTC_DATETIME),
    Column("data", DOCUMENT, nullable=False),
    PrimaryKeyConstraint("sequence", name="pk_human_messages"),
    UniqueConstraint("command_id", name="uq_human_messages_command_id"),
    ForeignKeyConstraint(
        ["command_id"],
        ["investigation_commands.command_id"],
        name="fk_human_messages_command_id_investigation_commands",
    ),
    ForeignKeyConstraint(
        ["session_id"],
        ["investigation_sessions.session_id"],
        name="fk_human_messages_session_id_investigation_sessions",
    ),
    Index(
        "ix_human_messages_session_sequence",
        "session_id",
        "sequence",
    ),
    **TABLE_OPTIONS,
)

human_requests = Table(
    "human_requests",
    metadata,
    Column("created_seq", BigInteger, primary_key=True, autoincrement=True),
    Column("request_id", String(120), nullable=False),
    Column("session_id", String(120), nullable=False),
    Column("status", String(32), nullable=False),
    Column("expires_at", UTC_DATETIME, nullable=False),
    Column("data", DOCUMENT, nullable=False),
    Column(
        "pending_session_id",
        String(120),
        Computed(
            "CASE WHEN status = 'pending' THEN session_id END",
            persisted=True,
        ),
    ),
    PrimaryKeyConstraint("created_seq", name="pk_human_requests"),
    UniqueConstraint("request_id", name="uq_human_requests_request_id"),
    ForeignKeyConstraint(
        ["session_id"],
        ["investigation_sessions.session_id"],
        name="fk_human_requests_session_id_investigation_sessions",
    ),
    Index("one_pending_request", "pending_session_id", unique=True),
    Index("ix_human_requests_session_status", "session_id", "status"),
    **TABLE_OPTIONS,
)

investigation_events = Table(
    "investigation_events",
    metadata,
    Column("sequence", BigInteger, primary_key=True, autoincrement=True),
    Column("event_id", String(120), nullable=False),
    Column("session_id", String(120), nullable=False),
    Column("event_type", String(80), nullable=False),
    Column("timestamp", UTC_DATETIME, nullable=False),
    Column("actor", String(120), nullable=False),
    Column("data", DOCUMENT, nullable=False),
    PrimaryKeyConstraint("sequence", name="pk_investigation_events"),
    UniqueConstraint("event_id", name="uq_investigation_events_event_id"),
    ForeignKeyConstraint(
        ["session_id"],
        ["investigation_sessions.session_id"],
        name="fk_investigation_events_session_id_investigation_sessions",
    ),
    Index(
        "ix_investigation_events_session_sequence",
        "session_id",
        "sequence",
    ),
    **TABLE_OPTIONS,
)

runtime_metadata = Table(
    "runtime_metadata",
    metadata,
    Column("meta_key", String(191), primary_key=True),
    Column("meta_value", VALUE, nullable=False),
    PrimaryKeyConstraint("meta_key", name="pk_runtime_metadata"),
    **TABLE_OPTIONS,
)

runtime_work = Table(
    "runtime_work",
    metadata,
    Column("kind", String(32), primary_key=True),
    Column("session_id", String(120), primary_key=True),
    Column("created_at", UTC_DATETIME, nullable=False),
    PrimaryKeyConstraint("kind", "session_id", name="pk_runtime_work"),
    ForeignKeyConstraint(
        ["session_id"],
        ["investigation_sessions.session_id"],
        name="fk_runtime_work_session_id_investigation_sessions",
    ),
    **TABLE_OPTIONS,
)

runtime_coordination = Table(
    "runtime_coordination",
    metadata,
    Column("name", String(64), primary_key=True),
    PrimaryKeyConstraint("name", name="pk_runtime_coordination"),
    **TABLE_OPTIONS,
)


def create_runtime_schema(engine):
    """Create the same tables for isolated tests; production uses Alembic."""
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            mysql_insert(runtime_coordination)
            .values(name="worker_claim")
            .prefix_with("IGNORE")
        )
