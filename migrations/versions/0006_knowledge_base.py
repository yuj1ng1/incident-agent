"""Create knowledge base document, version, and ngram-indexed chunk tables."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0006_knowledge_base"
down_revision = "0005_remediation_store"
branch_labels = None
depends_on = None

TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_bin",
}
SEARCH_TEXT_COLLATION = "utf8mb4_0900_ai_ci"
UTC_DATETIME = mysql.DATETIME(fsp=6)


def upgrade():
    op.create_table(
        "knowledge_documents",
        sa.Column("doc_pk", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.String(120), nullable=False),
        sa.Column("service", sa.String(200), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("source", sa.String(300), nullable=False),
        sa.Column("published_version_id", sa.String(120), nullable=True),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.Column("updated_at", UTC_DATETIME, nullable=False),
        sa.PrimaryKeyConstraint("doc_pk", name="pk_knowledge_documents"),
        sa.UniqueConstraint(
            "document_id",
            name="uq_knowledge_documents_document_id",
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_knowledge_documents_service",
        "knowledge_documents",
        ["service"],
    )
    op.create_index(
        "ix_knowledge_documents_published_version",
        "knowledge_documents",
        ["published_version_id"],
    )

    op.create_table(
        "knowledge_versions",
        sa.Column("version_pk", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("version_id", sa.String(120), nullable=False),
        sa.Column("document_id", sa.String(120), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("raw_markdown", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.PrimaryKeyConstraint("version_pk", name="pk_knowledge_versions"),
        sa.UniqueConstraint(
            "version_id",
            name="uq_knowledge_versions_version_id",
        ),
        sa.UniqueConstraint(
            "document_id",
            "content_sha256",
            name="uq_knowledge_versions_document_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["knowledge_documents.document_id"],
            name="fk_knowledge_versions_document_id_knowledge_documents",
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_knowledge_versions_document_status",
        "knowledge_versions",
        ["document_id", "status"],
    )
    op.create_index(
        "ix_knowledge_versions_document_created",
        "knowledge_versions",
        ["document_id", "created_at"],
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("chunk_pk", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chunk_id", sa.String(120), nullable=False),
        sa.Column("version_id", sa.String(120), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column(
            "heading_path",
            sa.String(500, collation=SEARCH_TEXT_COLLATION),
            nullable=False,
        ),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column(
            "chunk_text",
            mysql.MEDIUMTEXT(collation=SEARCH_TEXT_COLLATION),
            nullable=False,
        ),
        sa.Column("embedding_model", sa.String(120), nullable=True),
        sa.Column("embedding_dim", sa.Integer(), nullable=True),
        sa.Column("embedding_vector", mysql.MEDIUMBLOB(), nullable=True),
        sa.PrimaryKeyConstraint("chunk_pk", name="pk_knowledge_chunks"),
        sa.UniqueConstraint(
            "chunk_id",
            name="uq_knowledge_chunks_chunk_id",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["knowledge_versions.version_id"],
            name="fk_knowledge_chunks_version_id_knowledge_versions",
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_knowledge_chunks_version_ordinal",
        "knowledge_chunks",
        ["version_id", "ordinal"],
    )
    op.create_index(
        "ix_knowledge_chunks_embedding",
        "knowledge_chunks",
        ["embedding_model", "embedding_dim"],
    )
    op.create_index(
        "ft_knowledge_chunks_heading_text",
        "knowledge_chunks",
        ["heading_path", "chunk_text"],
        mysql_prefix="FULLTEXT",
        mysql_with_parser="ngram",
    )


def downgrade():
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_versions")
    op.drop_table("knowledge_documents")
