"""Regulatory intelligence: detected changes, the baselines they are measured against, and the
watches that route them to people.

The event table is an append-only record of things that happened at a point in time - an
approval, a label revision, a trial stopping. Storing events does not conflict with answering
chat questions from live sources: an event is a historical fact, not a cached answer.
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin

EVENT_TYPES = (
    "new_drug_approval",
    "generic_approval",
    "new_indication",
    "manufacturing_change",
    "safety_program_change",
    "bioequivalence_supplement",
    "labeling_supplement",
    "other_supplement",
    "label_change",
    "trial_status_change",
    "trial_registered",
    "trial_results_posted",
)
SIGNIFICANCE_LEVELS = ("high", "medium", "low")
MONITOR_RUN_STATUSES = ("running", "completed", "failed", "skipped")
DELIVERY_CHANNELS = ("email", "slack")


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"


class RegulatoryEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "regulatory_events"
    __table_args__ = (
        # Detectors re-scan overlapping windows because sources publish with a lag; this key
        # is what makes re-scanning free instead of duplicating the feed.
        UniqueConstraint("dedup_key"),
        CheckConstraint(f"event_type IN {_in(EVENT_TYPES)}", name="event_type_values"),
        CheckConstraint(f"significance IN {_in(SIGNIFICANCE_LEVELS)}", name="significance_values"),
        Index("ix_regulatory_events_occurred_on_id", "occurred_on", "id"),
        Index("ix_regulatory_events_significance_occurred_on", "significance", "occurred_on"),
        Index("ix_regulatory_events_event_type_occurred_on", "event_type", "occurred_on"),
        Index("ix_regulatory_events_detected_at", "detected_at"),
        Index("ix_regulatory_events_search_vector", "search_vector", postgresql_using="gin"),
    )

    dedup_key: Mapped[str] = mapped_column(String(512), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    significance: Mapped[str] = mapped_column(String(16), nullable=False)
    headline: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    drug_names: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    sponsor: Mapped[str | None] = mapped_column(String(255))
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    source_url: Mapped[str] = mapped_column(String(2_048), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    provenance: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', headline || ' ' || subject || ' ' || "
            "coalesce(sponsor, '') || ' ' || summary)",
            persisted=True,
        ),
        nullable=False,
    )


class SourceSnapshot(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The last observed state of a tracked record, used as the baseline for change detection.

    Sources such as openFDA serve only the current version of a label. Knowing what changed
    therefore requires remembering what was there before.
    """

    __tablename__ = "source_snapshots"
    __table_args__ = (UniqueConstraint("source", "entity_key"),)

    source: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_key: Mapped[str] = mapped_column(String(255), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MonitorRun(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "monitor_runs"
    __table_args__ = (
        CheckConstraint(f"status IN {_in(MONITOR_RUN_STATUSES)}", name="status_values"),
        Index("ix_monitor_runs_detector_started_at", "detector", "started_at"),
    )

    detector: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    window_start: Mapped[date] = mapped_column(Date, nullable=False)
    window_end: Mapped[date] = mapped_column(Date, nullable=False)
    records_scanned: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    events_created: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    baselines_recorded: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(String(500))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Watch(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "watches"
    __table_args__ = (
        CheckConstraint(
            f"min_significance IN {_in(SIGNIFICANCE_LEVELS)}", name="min_significance_values"
        ),
        Index("ix_watches_user_id", "user_id"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    terms: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    event_types: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    min_significance: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="medium"
    )
    notify_email: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    # A Slack incoming-webhook URL is a bearer credential. It is never returned by the API.
    slack_webhook_url: Mapped[str | None] = mapped_column(String(512))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_delivery_error: Mapped[str | None] = mapped_column(String(500))


class WatchDelivery(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Records that an event reached a watch on a channel, so it is never sent twice."""

    __tablename__ = "watch_deliveries"
    __table_args__ = (
        UniqueConstraint("watch_id", "event_id", "channel"),
        CheckConstraint(f"channel IN {_in(DELIVERY_CHANNELS)}", name="channel_values"),
    )

    watch_id: Mapped[UUID] = mapped_column(
        ForeignKey("watches.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[UUID] = mapped_column(
        ForeignKey("regulatory_events.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
