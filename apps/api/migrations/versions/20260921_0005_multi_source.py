"""Generalise data_sources.kind from source names to source categories.

The original constraint enumerated specific sources ('openfda'), which meant every new
upstream source required a migration. Categories describe how a source is reached; the
specific source stays in the unique 'slug' column.

This also repairs the constraint's name. The original migration passed a name that already
carried the 'ck_' prefix, and the metadata naming convention applied the prefix again, leaving
'ck_data_sources_ck_data_sources_kind_values' in the database while SQLAlchemy's metadata
expected 'ck_data_sources_kind_values'.

Revision ID: 20260921_0005
Revises: 20260717_0004
Create Date: 2026-09-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260921_0005"
down_revision: str | None = "20260717_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_NAME = "ck_data_sources_ck_data_sources_kind_values"
CORRECT_NAME = "ck_data_sources_kind_values"
OLD_KINDS = "('openfda', 'upload', 'website', 'internal')"
NEW_KINDS = "('api', 'upload', 'website', 'internal')"


def upgrade() -> None:
    op.execute(f"ALTER TABLE data_sources DROP CONSTRAINT IF EXISTS {LEGACY_NAME}")
    op.execute(f"ALTER TABLE data_sources DROP CONSTRAINT IF EXISTS {CORRECT_NAME}")
    op.execute("UPDATE data_sources SET kind = 'api' WHERE kind = 'openfda'")
    op.execute(
        f"ALTER TABLE data_sources ADD CONSTRAINT {CORRECT_NAME} CHECK (kind IN {NEW_KINDS})"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE data_sources DROP CONSTRAINT IF EXISTS {CORRECT_NAME}")
    op.execute("UPDATE data_sources SET kind = 'openfda' WHERE kind = 'api'")
    op.execute(
        f"ALTER TABLE data_sources ADD CONSTRAINT {LEGACY_NAME} CHECK (kind IN {OLD_KINDS})"
    )
