"""Create regulatory intelligence tables: events, baselines, monitor runs, and watches.

Every constraint and index name is wrapped in op.f() so it is used verbatim. Earlier migrations
passed names that already carried the naming-convention prefix, and Alembic applied the prefix
a second time; op.f() marks a name as final so these match the model metadata exactly.

Revision ID: 20260924_0006
Revises: 20260921_0005
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260924_0006"
down_revision: str | None = "20260921_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())

EVENT_TYPES = (
    "'new_drug_approval', 'generic_approval', 'new_indication', 'manufacturing_change', "
    "'safety_program_change', 'bioequivalence_supplement', 'labeling_supplement', "
    "'other_supplement', 'label_change', 'trial_status_change', 'trial_registered', "
    "'trial_results_posted'"
)


def _id() -> sa.Column[object]:
    return sa.Column("id", UUID, server_default=sa.text("uuidv7()"), nullable=False)


def _created() -> sa.Column[object]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def _updated() -> sa.Column[object]:
    return sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "regulatory_events",
        _id(),
        sa.Column("dedup_key", sa.String(512), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("significance", sa.String(16), nullable=False),
        sa.Column("headline", sa.String(500), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("drug_names", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("sponsor", sa.String(255)),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("source_url", sa.String(2_048), nullable=False),
        sa.Column("details", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("provenance", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', headline || ' ' || subject || ' ' || "
                "coalesce(sponsor, '') || ' ' || summary)",
                persisted=True,
            ),
            nullable=False,
        ),
        _created(),
        sa.CheckConstraint(
            f"event_type IN ({EVENT_TYPES})", name=op.f("ck_regulatory_events_event_type_values")
        ),
        sa.CheckConstraint(
            "significance IN ('high', 'medium', 'low')",
            name=op.f("ck_regulatory_events_significance_values"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_regulatory_events")),
        sa.UniqueConstraint("dedup_key", name=op.f("uq_regulatory_events_dedup_key")),
    )
    op.create_index(
        op.f("ix_regulatory_events_occurred_on_id"), "regulatory_events", ["occurred_on", "id"]
    )
    op.create_index(
        op.f("ix_regulatory_events_significance_occurred_on"),
        "regulatory_events",
        ["significance", "occurred_on"],
    )
    op.create_index(
        op.f("ix_regulatory_events_event_type_occurred_on"),
        "regulatory_events",
        ["event_type", "occurred_on"],
    )
    op.create_index(
        op.f("ix_regulatory_events_detected_at"), "regulatory_events", ["detected_at"]
    )
    op.create_index(
        op.f("ix_regulatory_events_search_vector"),
        "regulatory_events",
        ["search_vector"],
        postgresql_using="gin",
    )

    op.create_table(
        "source_snapshots",
        _id(),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("entity_key", sa.String(255), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("state", JSONB, nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        _created(),
        _updated(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_snapshots")),
        sa.UniqueConstraint("source", "entity_key", name=op.f("uq_source_snapshots_source")),
    )

    op.create_table(
        "monitor_runs",
        _id(),
        sa.Column("detector", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=False),
        sa.Column("window_end", sa.Date(), nullable=False),
        sa.Column("records_scanned", sa.Integer(), server_default="0", nullable=False),
        sa.Column("events_created", sa.Integer(), server_default="0", nullable=False),
        sa.Column("baselines_recorded", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.String(500)),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'skipped')",
            name=op.f("ck_monitor_runs_status_values"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monitor_runs")),
    )
    op.create_index(
        op.f("ix_monitor_runs_detector_started_at"), "monitor_runs", ["detector", "started_at"]
    )

    op.create_table(
        "watches",
        _id(),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("terms", JSONB, nullable=False),
        sa.Column("event_types", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("min_significance", sa.String(16), server_default="medium", nullable=False),
        sa.Column("notify_email", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("slack_webhook_url", sa.String(512)),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("last_notified_at", sa.DateTime(timezone=True)),
        sa.Column("last_delivery_error", sa.String(500)),
        _created(),
        _updated(),
        sa.CheckConstraint(
            "min_significance IN ('high', 'medium', 'low')",
            name=op.f("ck_watches_min_significance_values"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_watches_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watches")),
    )
    op.create_index(op.f("ix_watches_user_id"), "watches", ["user_id"])

    op.create_table(
        "watch_deliveries",
        _id(),
        sa.Column("watch_id", UUID, nullable=False),
        sa.Column("event_id", UUID, nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        _created(),
        sa.CheckConstraint(
            "channel IN ('email', 'slack')", name=op.f("ck_watch_deliveries_channel_values")
        ),
        sa.ForeignKeyConstraint(
            ["watch_id"],
            ["watches.id"],
            name=op.f("fk_watch_deliveries_watch_id_watches"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["regulatory_events.id"],
            name=op.f("fk_watch_deliveries_event_id_regulatory_events"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watch_deliveries")),
        sa.UniqueConstraint(
            "watch_id", "event_id", "channel", name=op.f("uq_watch_deliveries_watch_id")
        ),
    )


def downgrade() -> None:
    op.drop_table("watch_deliveries")
    op.drop_index(op.f("ix_watches_user_id"), table_name="watches")
    op.drop_table("watches")
    op.drop_index(op.f("ix_monitor_runs_detector_started_at"), table_name="monitor_runs")
    op.drop_table("monitor_runs")
    op.drop_table("source_snapshots")
    for name in (
        "ix_regulatory_events_search_vector",
        "ix_regulatory_events_detected_at",
        "ix_regulatory_events_event_type_occurred_on",
        "ix_regulatory_events_significance_occurred_on",
        "ix_regulatory_events_occurred_on_id",
    ):
        op.drop_index(op.f(name), table_name="regulatory_events")
    op.drop_table("regulatory_events")
