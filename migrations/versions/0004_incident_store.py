"""Create MySQL Incident and history-archive tables."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0004_incident_store"
down_revision = "0003_runtime_documents_text"
branch_labels = None
depends_on = None

TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_bin",
}
DOCUMENT = mysql.MEDIUMTEXT()
VALUE = mysql.TEXT()
UTC_DATETIME = mysql.DATETIME(fsp=6)


def upgrade():
    op.create_table(
        "incidents",
        sa.Column("created_seq", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("incident_id", sa.String(120), nullable=False),
        sa.Column("fingerprint", sa.String(191), nullable=True),
        sa.Column("service", sa.String(200), nullable=False),
        sa.Column("status", sa.String(100), nullable=False),
        sa.Column("alert_state", sa.String(100), nullable=False),
        sa.Column("cleared_at", UTC_DATETIME, nullable=True),
        sa.Column("recovery_verified_at", UTC_DATETIME, nullable=True),
        sa.Column("recovery_status", sa.String(32), nullable=True),
        sa.Column("recovery_next_retry_at", UTC_DATETIME, nullable=True),
        sa.Column("data", DOCUMENT, nullable=False),
        sa.Column("updated_at", UTC_DATETIME, nullable=False),
        sa.PrimaryKeyConstraint("created_seq", name="pk_incidents"),
        sa.UniqueConstraint("incident_id", name="uq_incidents_incident_id"),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_incidents_fingerprint_created",
        "incidents",
        ["fingerprint", "created_seq"],
    )
    op.create_index(
        "ix_incidents_status_created",
        "incidents",
        ["status", "created_seq"],
    )
    op.create_index("ix_incidents_service", "incidents", ["service"])
    op.create_index(
        "ix_incidents_recovery",
        "incidents",
        ["status", "alert_state", "recovery_verified_at"],
    )

    op.create_table(
        "incident_active",
        sa.Column("fingerprint", sa.String(191), nullable=False),
        sa.Column("incident_id", sa.String(120), nullable=False),
        sa.Column("updated_at", UTC_DATETIME, nullable=False),
        sa.PrimaryKeyConstraint("fingerprint", name="pk_incident_active"),
        sa.UniqueConstraint(
            "incident_id",
            name="uq_incident_active_incident_id",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.incident_id"],
            name="fk_incident_active_incident_id_incidents",
        ),
        **TABLE_OPTIONS,
    )

    op.create_table(
        "incident_history_archives",
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("incident_id", sa.String(120), nullable=False),
        sa.Column("session_ids", VALUE, nullable=False),
        sa.Column("body", DOCUMENT, nullable=False),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.PrimaryKeyConstraint("sha256", name="pk_incident_history_archives"),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_incident_history_archives_incident",
        "incident_history_archives",
        ["incident_id"],
    )


def downgrade():
    op.drop_table("incident_history_archives")
    op.drop_table("incident_active")
    op.drop_table("incidents")
