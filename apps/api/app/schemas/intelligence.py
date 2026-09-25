from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.intelligence.matching import normalize_terms
from app.intelligence.notifications import validate_slack_webhook
from app.intelligence.types import EventType, Significance


class RegulatoryEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source: str
    event_type: EventType
    significance: Significance
    headline: str
    summary: str
    subject: str
    drug_names: list[str]
    sponsor: str | None
    occurred_on: date
    detected_at: datetime
    source_url: str
    details: dict[str, Any]
    provenance: dict[str, Any]


class SignificanceCounts(BaseModel):
    high: int = 0
    medium: int = 0
    low: int = 0


class EventFeedResponse(BaseModel):
    events: list[RegulatoryEventResponse]
    next_cursor: str | None
    last_7_days: SignificanceCounts


class WatchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    terms: list[str] = Field(min_length=1, max_length=25)
    event_types: list[EventType] = Field(default_factory=list)
    min_significance: Significance = "medium"
    notify_email: bool = True
    slack_webhook_url: str | None = Field(default=None, max_length=512)

    @field_validator("terms")
    @classmethod
    def _terms(cls, value: list[str]) -> list[str]:
        cleaned = normalize_terms(value)
        if not cleaned:
            raise ValueError("Add at least one drug, company, or condition to watch")
        return cleaned

    @field_validator("slack_webhook_url")
    @classmethod
    def _slack(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return validate_slack_webhook(value)


class WatchUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    terms: list[str] | None = Field(default=None, min_length=1, max_length=25)
    event_types: list[EventType] | None = None
    min_significance: Significance | None = None
    notify_email: bool | None = None
    slack_webhook_url: str | None = Field(default=None, max_length=512)
    clear_slack: bool = False
    active: bool | None = None

    @field_validator("terms")
    @classmethod
    def _terms(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = normalize_terms(value)
        if not cleaned:
            raise ValueError("Add at least one drug, company, or condition to watch")
        return cleaned

    @field_validator("slack_webhook_url")
    @classmethod
    def _slack(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return validate_slack_webhook(value)


class WatchResponse(BaseModel):
    id: UUID
    name: str
    terms: list[str]
    event_types: list[str]
    min_significance: Significance
    notify_email: bool
    # The webhook itself is a credential and is never returned.
    slack_configured: bool
    slack_webhook_hint: str | None
    active: bool
    last_notified_at: datetime | None
    last_delivery_error: str | None
    created_at: datetime


class MonitorRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    detector: str
    status: Literal["running", "completed", "failed", "skipped"]
    window_start: date
    window_end: date
    records_scanned: int
    events_created: int
    baselines_recorded: int
    error: str | None
    started_at: datetime
    completed_at: datetime | None


class MonitorTriggerResponse(BaseModel):
    status: Literal["started"]
    message: str


class RegulatoryEventBrief(BaseModel):
    """A detected change, compact enough to hand to the model."""

    model_config = ConfigDict(from_attributes=True)

    headline: str
    summary: str
    significance: Significance
    event_type: EventType
    source: str
    occurred_on: date
    source_url: str
    sponsor: str | None
    drug_names: list[str]


class RegulatoryEventSearchResult(BaseModel):
    query: str | None
    days: int
    returned: int
    events: list[RegulatoryEventBrief]
    caveats: list[str]
