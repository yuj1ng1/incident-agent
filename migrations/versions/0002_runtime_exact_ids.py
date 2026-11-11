"""Preserve SQLite's exact comparisons for Runtime identifiers."""
from alembic import op

revision = "0002_runtime_exact_ids"
down_revision = "0001_runtime_store"
branch_labels = None
depends_on = None

TABLES = (
    "investigation_sessions",
    "investigation_checkpoints",
    "investigation_commands",
    "human_messages",
    "human_requests",
    "investigation_events",
    "runtime_metadata",
    "runtime_work",
    "runtime_coordination",
)


def _convert(collation):
    # Session-scoped setting. Referencing and referenced columns must be
    # converted together while application writers are stopped.
    op.execute("SET SESSION FOREIGN_KEY_CHECKS=0")
    try:
        for table in TABLES:
            op.execute(f"ALTER TABLE {table} CONVERT TO CHARACTER SET utf8mb4 COLLATE {collation}")
    finally:
        op.execute("SET SESSION FOREIGN_KEY_CHECKS=1")


def upgrade():
    _convert("utf8mb4_0900_bin")


def downgrade():
    _convert("utf8mb4_bin")
