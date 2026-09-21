from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    model_preferences: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    status: Literal["active", "archived"] | None = None

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class ConversationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str | None
    status: Literal["active", "archived"]
    model_preferences: dict[str, Any]
    active_leaf_message_id: UUID | None
    created_at: datetime
    updated_at: datetime


class MessageCreate(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    parent_message_id: UUID | None = None

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("message text cannot be blank")
        return normalized


class MessageEdit(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("message text cannot be blank")
        return normalized


class ChatTurnRequest(MessageCreate):
    provider: Literal["openai", "groq"] | None = None
    model: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("model")
    @classmethod
    def normalize_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("model cannot be blank")
        return normalized


class RegenerateRequest(BaseModel):
    provider: Literal["openai", "groq"] | None = None
    model: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("model")
    @classmethod
    def normalize_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("model cannot be blank")
        return normalized


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    parent_message_id: UUID | None
    supersedes_message_id: UUID | None
    role: Literal["user", "assistant", "system", "tool"]
    status: Literal["pending", "streaming", "completed", "failed", "cancelled"]
    content: list[dict[str, Any]]
    plain_text: str
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationResponse):
    messages: list[MessageResponse]
