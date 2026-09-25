from typing import Any, Protocol

from app.llm.models import (
    LLMCompletion,
    LLMMessage,
    LLMToolDefinition,
    LLMToolOutput,
)


class LLMConfigurationError(RuntimeError):
    """Raised when a selected provider has incomplete configuration."""


class LLMProviderError(RuntimeError):
    """Structured provider failure safe for internal persistence and optional dev display."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_error",
        retryable: bool = False,
        status_code: int | None = None,
        request_id: str | None = None,
        retry_count: int = 0,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        self.request_id = request_id
        self.retry_count = retry_count
        self.details = details or {}

    def diagnostics(self) -> dict[str, Any]:
        diagnostics: dict[str, Any] = {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "retry_count": self.retry_count,
        }
        if self.status_code is not None:
            diagnostics["status_code"] = self.status_code
        if self.request_id is not None:
            diagnostics["request_id"] = self.request_id
        if self.details:
            diagnostics["details"] = self.details
        return diagnostics


class LLMProvider(Protocol):
    async def complete(
        self,
        *,
        model: str,
        system_prompt: str,
        messages: list[LLMMessage],
        tools: list[LLMToolDefinition],
        continuation: dict[str, object] | None = None,
        tool_outputs: list[LLMToolOutput] | None = None,
        allow_web_search: bool = False,
    ) -> LLMCompletion: ...
