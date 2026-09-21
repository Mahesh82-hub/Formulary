from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class AssistantRun(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "assistant_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'awaiting_approval', 'completed', 'failed', "
            "'cancelled')",
            name="status_values",
        ),
        CheckConstraint("attempt > 0", name="attempt_positive"),
        Index("ix_assistant_runs_conversation_id_created_at", "conversation_id", "created_at"),
        Index("ix_assistant_runs_status", "status"),
    )

    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    trigger_message_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    response_message_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    retry_of_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assistant_runs.id", ondelete="SET NULL")
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'queued'"),
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    orchestration_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    usage: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ToolExecution(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "tool_executions"
    __table_args__ = (
        CheckConstraint(
            "tool_kind IN ('internal', 'skill', 'mcp', 'external_api')",
            name="tool_kind_values",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'awaiting_approval', 'completed', 'failed', "
            "'cancelled')",
            name="status_values",
        ),
        Index("ix_tool_executions_run_id_created_at", "run_id", "created_at"),
        Index("ix_tool_executions_tool_name", "tool_name"),
    )

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("assistant_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    tool_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_call_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'queued'"),
    )
    arguments: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    requires_approval: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    approved_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
