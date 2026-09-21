from typing import Any, Literal

from pydantic import BaseModel, Field


class LLMMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class LLMToolDefinition(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class LLMToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class LLMToolOutput(BaseModel):
    call_id: str
    name: str
    output: Any
    is_error: bool = False


class LLMCompletion(BaseModel):
    provider_response_id: str
    text: str = ""
    tool_calls: list[LLMToolCall] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    continuation: dict[str, Any] = Field(default_factory=dict)
