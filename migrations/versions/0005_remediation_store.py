"""Create the authoritative transactional Remediation document."""
import json
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0005_remediation_store"
down_revision = "0004_incident_store"
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table(
        "remediation_state",
        sa.Column("id", sa.SmallInteger(), nullable=False, autoincrement=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("data", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_remediation_state"),
        sa.CheckConstraint("id = 1", name="ck_remediation_state_singleton"),
        sa.CheckConstraint("revision >= 0", name="ck_remediation_state_revision"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
    )
    op.bulk_insert(table, [{
        "id": 1,
        "revision": 0,
        "data": json.dumps({
            "version": 2,
            "proposals": {},
            "actions": [],
            "verifications": [],
            "audit": [],
        }),
        "updated_at": datetime.now(timezone.utc).replace(tzinfo=None),
    }])


def downgrade():
    # Avoid silently discarding approvals/results. Operators must export and
    # explicitly empty this table before rolling back an in-use store.
    row = op.get_bind().execute(sa.text(
        "SELECT revision, data FROM remediation_state WHERE id = 1"
    )).mappings().first()
    empty = row is None
    if row is not None and row["revision"] == 0:
        try:
            empty = json.loads(row["data"]) == {
                "version": 2, "proposals": {}, "actions": [],
                "verifications": [], "audit": [],
            }
        except (ValueError, TypeError):
            empty = False
    if not empty:
        raise RuntimeError("remediation_downgrade_requires_export_and_empty_store")
    op.drop_table("remediation_state")
