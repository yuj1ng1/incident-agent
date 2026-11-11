"""Store Runtime documents as text so JSON key order is preserved."""
from alembic import op

revision = "0003_runtime_documents_text"
down_revision = "0002_runtime_exact_ids"
branch_labels = None
depends_on = None

DOCUMENT_TABLES = (
    "investigation_sessions",
    "investigation_checkpoints",
    "investigation_commands",
    "human_messages",
    "human_requests",
    "investigation_events",
)


def upgrade():
    for table in DOCUMENT_TABLES:
        op.execute(f"ALTER TABLE {table} MODIFY data MEDIUMTEXT NOT NULL")
    op.execute("ALTER TABLE runtime_metadata MODIFY meta_value TEXT NOT NULL")


def downgrade():
    for table in DOCUMENT_TABLES:
        op.execute(f"ALTER TABLE {table} MODIFY data JSON NOT NULL")
    op.execute("ALTER TABLE runtime_metadata MODIFY meta_value JSON NOT NULL")
