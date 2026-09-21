"""Create core email authentication and branching chat tables.

Revision ID: 20260715_0002
Revises: 20260715_0001
Create Date: 2026-07-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260715_0002"
down_revision: str | None = "20260715_0001"
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
        "users",
        uuid_primary_key(),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        created_at_column(),
        updated_at_column(),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_users_status_values"),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    op.create_table(
        "otp_challenges",
        uuid_primary_key(),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("purpose", sa.String(length=32), server_default="login", nullable=False),
        sa.Column("code_digest", sa.String(length=64), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="5", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_ip_hash", sa.String(length=64), nullable=True),
        sa.Column("user_agent_hash", sa.String(length=64), nullable=True),
        created_at_column(),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_otp_challenges_attempt_count_nonnegative"
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_otp_challenges_expiry_after_creation"
        ),
        sa.CheckConstraint("max_attempts > 0", name="ck_otp_challenges_max_attempts_positive"),
        sa.CheckConstraint("purpose IN ('login')", name="ck_otp_challenges_purpose_values"),
        sa.PrimaryKeyConstraint("id", name="pk_otp_challenges"),
    )
    op.create_index(
        "ix_otp_challenges_email_created_at",
        "otp_challenges",
        ["email", "created_at"],
    )
    op.create_index("ix_otp_challenges_expires_at", "otp_challenges", ["expires_at"])

    op.create_table(
        "user_sessions",
        uuid_primary_key(),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_hash", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        created_at_column(),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_user_sessions_expiry_after_creation"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_sessions_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_sessions"),
        sa.UniqueConstraint("token_digest", name="uq_user_sessions_token_digest"),
    )
    op.create_index(
        "ix_user_sessions_user_id_expires_at",
        "user_sessions",
        ["user_id", "expires_at"],
    )

    op.create_table(
        "auth_events",
        uuid_primary_key(),
        sa.Column("user_id", UUID, nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("ip_hash", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("metadata", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        created_at_column(),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_auth_events_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_auth_events"),
    )
    op.create_index(
        "ix_auth_events_email_created_at",
        "auth_events",
        ["email", "created_at"],
    )
    op.create_index(
        "ix_auth_events_user_id_created_at",
        "auth_events",
        ["user_id", "created_at"],
    )

    op.create_table(
        "conversations",
        uuid_primary_key(),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("active_leaf_message_id", UUID, nullable=True),
        sa.Column(
            "model_preferences",
            JSONB,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        created_at_column(),
        updated_at_column(),
        sa.CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_conversations_status_values",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_conversations_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_conversations"),
    )
    op.create_index(
        "ix_conversations_user_id_updated_at",
        "conversations",
        ["user_id", "updated_at"],
    )

    op.create_table(
        "messages",
        uuid_primary_key(),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("parent_message_id", UUID, nullable=True),
        sa.Column("supersedes_message_id", UUID, nullable=True),
        sa.Column("created_by_user_id", UUID, nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("content", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("plain_text", sa.Text(), server_default="", nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        created_at_column(),
        updated_at_column(),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'system', 'tool')",
            name="ck_messages_role_values",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'streaming', 'completed', 'failed', 'cancelled')",
            name="ck_messages_status_values",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_messages_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_messages_created_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["parent_message_id"],
            ["messages.id"],
            name="fk_messages_parent_message_id_messages",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_message_id"],
            ["messages.id"],
            name="fk_messages_supersedes_message_id_messages",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
    )
    op.create_index(
        "ix_messages_conversation_id_created_at",
        "messages",
        ["conversation_id", "created_at"],
    )
    op.create_index("ix_messages_parent_message_id", "messages", ["parent_message_id"])
    op.create_index(
        "ix_messages_supersedes_message_id",
        "messages",
        ["supersedes_message_id"],
    )
    op.create_foreign_key(
        "fk_conversations_active_leaf_message_id_messages",
        "conversations",
        "messages",
        ["active_leaf_message_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "assistant_runs",
        uuid_primary_key(),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("trigger_message_id", UUID, nullable=True),
        sa.Column("response_message_id", UUID, nullable=True),
        sa.Column("retry_of_run_id", UUID, nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="1", nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column(
            "orchestration_state",
            JSONB,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("usage", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("error", JSONB, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        created_at_column(),
        sa.CheckConstraint("attempt > 0", name="ck_assistant_runs_attempt_positive"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'awaiting_approval', 'completed', 'failed', "
            "'cancelled')",
            name="ck_assistant_runs_status_values",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_assistant_runs_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["response_message_id"],
            ["messages.id"],
            name="fk_assistant_runs_response_message_id_messages",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["retry_of_run_id"],
            ["assistant_runs.id"],
            name="fk_assistant_runs_retry_of_run_id_assistant_runs",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["trigger_message_id"],
            ["messages.id"],
            name="fk_assistant_runs_trigger_message_id_messages",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_assistant_runs"),
        sa.UniqueConstraint("idempotency_key", name="uq_assistant_runs_idempotency_key"),
    )
    op.create_index(
        "ix_assistant_runs_conversation_id_created_at",
        "assistant_runs",
        ["conversation_id", "created_at"],
    )
    op.create_index("ix_assistant_runs_status", "assistant_runs", ["status"])

    op.create_table(
        "tool_executions",
        uuid_primary_key(),
        sa.Column("run_id", UUID, nullable=False),
        sa.Column("tool_kind", sa.String(length=32), nullable=False),
        sa.Column("tool_name", sa.String(length=255), nullable=False),
        sa.Column("provider_call_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("arguments", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("result", JSONB, nullable=True),
        sa.Column("error", JSONB, nullable=True),
        sa.Column(
            "requires_approval", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("approved_by_user_id", UUID, nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        created_at_column(),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'awaiting_approval', 'completed', 'failed', "
            "'cancelled')",
            name="ck_tool_executions_status_values",
        ),
        sa.CheckConstraint(
            "tool_kind IN ('internal', 'skill', 'mcp', 'external_api')",
            name="ck_tool_executions_tool_kind_values",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"],
            ["users.id"],
            name="fk_tool_executions_approved_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["assistant_runs.id"],
            name="fk_tool_executions_run_id_assistant_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tool_executions"),
    )
    op.create_index(
        "ix_tool_executions_run_id_created_at",
        "tool_executions",
        ["run_id", "created_at"],
    )
    op.create_index("ix_tool_executions_tool_name", "tool_executions", ["tool_name"])


def downgrade() -> None:
    op.drop_table("tool_executions")
    op.drop_table("assistant_runs")
    op.drop_constraint(
        "fk_conversations_active_leaf_message_id_messages",
        "conversations",
        type_="foreignkey",
    )
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("auth_events")
    op.drop_table("user_sessions")
    op.drop_table("otp_challenges")
    op.drop_table("users")
