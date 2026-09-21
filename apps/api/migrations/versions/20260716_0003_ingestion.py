"""Create durable source-ingestion and full-text chunk tables.

Revision ID: 20260716_0003
Revises: 20260715_0002
Create Date: 2026-07-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260716_0003"
down_revision: str | None = "20260715_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def uuid_primary_key() -> sa.Column[object]:
    return sa.Column("id", UUID, server_default=sa.text("uuidv7()"), nullable=False)


def created_at_column() -> sa.Column[object]:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def updated_at_column() -> sa.Column[object]:
    return sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "data_sources",
        uuid_primary_key(),
        sa.Column("user_id", UUID, nullable=True),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("config", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        created_at_column(),
        updated_at_column(),
        sa.CheckConstraint(
            "kind IN ('openfda', 'upload', 'website', 'internal')",
            name="ck_data_sources_kind_values",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_data_sources_status_values",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_data_sources_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_data_sources"),
        sa.UniqueConstraint("slug", name="uq_data_sources_slug"),
    )
    op.create_index("ix_data_sources_user_id_updated_at", "data_sources", ["user_id", "updated_at"])

    op.create_table(
        "documents",
        uuid_primary_key(),
        sa.Column("user_id", UUID, nullable=True),
        sa.Column("data_source_id", UUID, nullable=False),
        sa.Column("external_key", sa.String(length=512), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("metadata", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        created_at_column(),
        updated_at_column(),
        sa.ForeignKeyConstraint(
            ["data_source_id"],
            ["data_sources.id"],
            name="fk_documents_data_source_id_data_sources",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_documents_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.UniqueConstraint(
            "data_source_id",
            "external_key",
            name="uq_documents_data_source_id_external_key",
        ),
    )
    op.create_index(
        "ix_documents_data_source_id_updated_at",
        "documents",
        ["data_source_id", "updated_at"],
    )
    op.create_index("ix_documents_user_id_updated_at", "documents", ["user_id", "updated_at"])

    op.create_table(
        "document_versions",
        uuid_primary_key(),
        sa.Column("document_id", UUID, nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("object_uri", sa.String(length=1000), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column(
            "extraction_status", sa.String(length=32), server_default="pending", nullable=False
        ),
        sa.Column("metadata", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        created_at_column(),
        sa.CheckConstraint("byte_size >= 0", name="ck_document_versions_byte_size_nonnegative"),
        sa.CheckConstraint(
            "extraction_status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_document_versions_extraction_status_values",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_document_versions_document_id_documents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_versions"),
        sa.UniqueConstraint(
            "document_id", "checksum", name="uq_document_versions_document_id_checksum"
        ),
        sa.UniqueConstraint(
            "document_id", "version", name="uq_document_versions_document_id_version"
        ),
    )
    op.create_index("ix_document_versions_checksum", "document_versions", ["checksum"])
    op.create_index(
        "ix_document_versions_document_id_created_at",
        "document_versions",
        ["document_id", "created_at"],
    )

    op.create_table(
        "document_chunks",
        uuid_primary_key(),
        sa.Column("document_version_id", UUID, nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("section_path", sa.String(length=1000), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("metadata", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', content)", persisted=True),
            nullable=False,
        ),
        created_at_column(),
        sa.CheckConstraint("chunk_index >= 0", name="ck_document_chunks_chunk_index_nonnegative"),
        sa.CheckConstraint("token_count > 0", name="ck_document_chunks_token_count_positive"),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name="fk_document_chunks_document_version_id_document_versions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_chunks"),
        sa.UniqueConstraint(
            "document_version_id",
            "chunk_index",
            name="uq_document_chunks_document_version_id_chunk_index",
        ),
    )
    op.create_index("ix_document_chunks_content_hash", "document_chunks", ["content_hash"])
    op.create_index(
        "ix_document_chunks_document_version_id_chunk_index",
        "document_chunks",
        ["document_version_id", "chunk_index"],
    )
    op.create_index(
        "ix_document_chunks_search_vector",
        "document_chunks",
        ["search_vector"],
        postgresql_using="gin",
    )

    op.create_table(
        "ingestion_jobs",
        uuid_primary_key(),
        sa.Column("user_id", UUID, nullable=True),
        sa.Column("data_source_id", UUID, nullable=True),
        sa.Column("document_id", UUID, nullable=True),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("request", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("progress", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="1", nullable=False),
        sa.Column("error", JSONB, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        created_at_column(),
        sa.CheckConstraint("attempt > 0", name="ck_ingestion_jobs_attempt_positive"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_ingestion_jobs_status_values",
        ),
        sa.ForeignKeyConstraint(
            ["data_source_id"],
            ["data_sources.id"],
            name="fk_ingestion_jobs_data_source_id_data_sources",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_ingestion_jobs_document_id_documents",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_ingestion_jobs_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ingestion_jobs"),
    )
    op.create_index(
        "ix_ingestion_jobs_status_created_at", "ingestion_jobs", ["status", "created_at"]
    )
    op.create_index(
        "ix_ingestion_jobs_user_id_created_at", "ingestion_jobs", ["user_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_ingestion_jobs_user_id_created_at", table_name="ingestion_jobs")
    op.drop_index("ix_ingestion_jobs_status_created_at", table_name="ingestion_jobs")
    op.drop_table("ingestion_jobs")
    op.drop_index("ix_document_chunks_search_vector", table_name="document_chunks")
    op.drop_index(
        "ix_document_chunks_document_version_id_chunk_index", table_name="document_chunks"
    )
    op.drop_index("ix_document_chunks_content_hash", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_index("ix_document_versions_document_id_created_at", table_name="document_versions")
    op.drop_index("ix_document_versions_checksum", table_name="document_versions")
    op.drop_table("document_versions")
    op.drop_index("ix_documents_user_id_updated_at", table_name="documents")
    op.drop_index("ix_documents_data_source_id_updated_at", table_name="documents")
    op.drop_table("documents")
    op.drop_index("ix_data_sources_user_id_updated_at", table_name="data_sources")
    op.drop_table("data_sources")
