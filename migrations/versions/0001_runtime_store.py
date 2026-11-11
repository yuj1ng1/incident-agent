"""Create the authoritative Runtime store schema.

Revision ID: 0001_runtime_store
Revises:
Create Date: 2026-11-11
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0001_runtime_store"
down_revision = None
branch_labels = None
depends_on = None

TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    # SQLite TEXT and JSON keys compare binary. Case-insensitive collation
    # would merge distinct command/request IDs and change idempotency.
    "mysql_collate": "utf8mb4_bin",
}
JSON = mysql.JSON()
UTC_DATETIME = mysql.DATETIME(fsp=6)


def upgrade():
    op.create_table(
        "investigation_sessions",
        # Insertion order replaces SQLite rowid for listing and latest-Session
        # selection, so the sequence is the physical key.
        sa.Column("created_seq", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(120), nullable=False),
        sa.Column("incident_id", sa.String(120), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("state_version", sa.BigInteger(), nullable=False),
        sa.Column("budget_group_id", sa.String(120), nullable=True),
        sa.Column("priority", sa.SmallInteger(), nullable=False),
        sa.Column("queued_at", UTC_DATETIME, nullable=False),
        sa.Column("lease_until", UTC_DATETIME, nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("data", JSON, nullable=False),
        # A nullable generated key lets a unique index enforce "one active
        # Session per Incident" without a partial index.
        sa.Column(
            "active_incident_id",
            sa.String(120),
            sa.Computed(
                "CASE WHEN status IN "
                "('COMPLETED','EXHAUSTED','FAILED','CANCELLED') "
                "THEN NULL ELSE incident_id END",
                persisted=True,
            ),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("created_seq", name="pk_investigation_sessions"),
        sa.UniqueConstraint(
            "session_id",
            name="uq_investigation_sessions_session_id",
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "one_active_incident",
        "investigation_sessions",
        ["active_incident_id"],
        unique=True,
    )
    op.create_index(
        "ix_investigation_sessions_incident_created",
        "investigation_sessions",
        ["incident_id", "created_seq"],
    )
    op.create_index(
        "ix_investigation_sessions_budget_group",
        "investigation_sessions",
        ["incident_id", "budget_group_id"],
    )
    op.create_index(
        "ix_investigation_sessions_status_queued",
        "investigation_sessions",
        ["status", "queued_at"],
    )

    op.create_table(
        "investigation_checkpoints",
        sa.Column("sequence", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("checkpoint_id", sa.String(120), nullable=False),
        sa.Column("session_id", sa.String(120), nullable=False),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.Column("data", JSON, nullable=False),
        sa.PrimaryKeyConstraint("sequence", name="pk_investigation_checkpoints"),
        sa.UniqueConstraint(
            "checkpoint_id",
            name="uq_investigation_checkpoints_checkpoint_id",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["investigation_sessions.session_id"],
            name="fk_investigation_checkpoints_session_id_investigation_sessions",
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_investigation_checkpoints_session_sequence",
        "investigation_checkpoints",
        ["session_id", "sequence"],
    )

    op.create_table(
        "investigation_commands",
        sa.Column("sequence", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("command_id", sa.String(120), nullable=False),
        sa.Column("session_id", sa.String(120), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        # Queried when cancel commands must run before other pending commands.
        sa.Column("command_type", sa.String(32), nullable=False),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.Column("data", JSON, nullable=False),
        sa.PrimaryKeyConstraint("sequence", name="pk_investigation_commands"),
        sa.UniqueConstraint(
            "command_id",
            name="uq_investigation_commands_command_id",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["investigation_sessions.session_id"],
            name="fk_investigation_commands_session_id_investigation_sessions",
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_investigation_commands_pending",
        "investigation_commands",
        ["status", "session_id"],
    )
    op.create_index(
        "ix_investigation_commands_session_sequence",
        "investigation_commands",
        ["session_id", "sequence"],
    )

    op.create_table(
        "human_messages",
        sa.Column("sequence", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("command_id", sa.String(120), nullable=False),
        sa.Column("session_id", sa.String(120), nullable=False),
        sa.Column("consumed_at", UTC_DATETIME, nullable=True),
        sa.Column("data", JSON, nullable=False),
        sa.PrimaryKeyConstraint("sequence", name="pk_human_messages"),
        sa.UniqueConstraint("command_id", name="uq_human_messages_command_id"),
        sa.ForeignKeyConstraint(
            ["command_id"],
            ["investigation_commands.command_id"],
            name="fk_human_messages_command_id_investigation_commands",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["investigation_sessions.session_id"],
            name="fk_human_messages_session_id_investigation_sessions",
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_human_messages_session_sequence",
        "human_messages",
        ["session_id", "sequence"],
    )

    op.create_table(
        "human_requests",
        sa.Column("created_seq", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(120), nullable=False),
        sa.Column("session_id", sa.String(120), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("expires_at", UTC_DATETIME, nullable=False),
        sa.Column("data", JSON, nullable=False),
        sa.Column(
            "pending_session_id",
            sa.String(120),
            sa.Computed(
                "CASE WHEN status = 'pending' THEN session_id END",
                persisted=True,
            ),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("created_seq", name="pk_human_requests"),
        sa.UniqueConstraint("request_id", name="uq_human_requests_request_id"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["investigation_sessions.session_id"],
            name="fk_human_requests_session_id_investigation_sessions",
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "one_pending_request",
        "human_requests",
        ["pending_session_id"],
        unique=True,
    )
    op.create_index(
        "ix_human_requests_session_status",
        "human_requests",
        ["session_id", "status"],
    )

    op.create_table(
        "investigation_events",
        sa.Column("sequence", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(120), nullable=False),
        sa.Column("session_id", sa.String(120), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("timestamp", UTC_DATETIME, nullable=False),
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("data", JSON, nullable=False),
        sa.PrimaryKeyConstraint("sequence", name="pk_investigation_events"),
        sa.UniqueConstraint("event_id", name="uq_investigation_events_event_id"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["investigation_sessions.session_id"],
            name="fk_investigation_events_session_id_investigation_sessions",
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_investigation_events_session_sequence",
        "investigation_events",
        ["session_id", "sequence"],
    )

    op.create_table(
        "runtime_metadata",
        sa.Column("meta_key", sa.String(191), nullable=False),
        sa.Column("meta_value", JSON, nullable=False),
        sa.PrimaryKeyConstraint("meta_key", name="pk_runtime_metadata"),
        **TABLE_OPTIONS,
    )

    op.create_table(
        "runtime_work",
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("session_id", sa.String(120), nullable=False),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.PrimaryKeyConstraint("kind", "session_id", name="pk_runtime_work"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["investigation_sessions.session_id"],
            name="fk_runtime_work_session_id_investigation_sessions",
        ),
        **TABLE_OPTIONS,
    )

    op.create_table(
        "runtime_coordination",
        sa.Column("name", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("name", name="pk_runtime_coordination"),
        **TABLE_OPTIONS,
    )
    op.execute(
        "INSERT INTO runtime_coordination (name) VALUES ('worker_claim')"
    )


def downgrade():
    op.drop_table("runtime_coordination")
    op.drop_table("runtime_work")
    op.drop_table("runtime_metadata")
    op.drop_table("investigation_events")
    op.drop_table("human_requests")
    op.drop_table("human_messages")
    op.drop_table("investigation_commands")
    op.drop_table("investigation_checkpoints")
    op.drop_table("investigation_sessions")
