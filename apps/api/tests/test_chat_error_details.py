from typing import cast
from uuid import uuid4

from pydantic import SecretStr

from app.core.config import Settings
from app.llm.provider import LLMProviderError
from app.mcp_gateway.client import FastMCPToolClient
from app.services.chat_orchestrator import ChatOrchestrator, CompletionGateway


def settings(*, app_env: str, expose: bool) -> Settings:
    return Settings(
        app_env=app_env,
        chat_expose_error_details=expose,
        postgres_db="database",
        postgres_user="user",
        postgres_password=SecretStr("secret-password"),
    )


def orchestrator(configuration: Settings) -> ChatOrchestrator:
    return ChatOrchestrator(
        cast(CompletionGateway, None),
        cast(FastMCPToolClient, None),
        configuration,
    )


def test_development_failure_event_includes_safe_provider_diagnostics() -> None:
    error = LLMProviderError(
        "The model provider is temporarily unavailable (HTTP 503)",
        code="provider_unavailable",
        retryable=True,
        status_code=503,
        request_id="groq-request-1",
        retry_count=1,
    )

    data = orchestrator(settings(app_env="development", expose=True))._failure_event_data(
        uuid4(), error
    )

    assert data["code"] == "provider_unavailable"
    assert data["message"] == "The model provider is temporarily unavailable (HTTP 503)"
    assert data["retryable"] is True
    assert data["retry_count"] == 1
    assert data["status_code"] == 503
    assert data["request_id"] == "groq-request-1"


def test_production_failure_event_suppresses_provider_diagnostics() -> None:
    error = LLMProviderError(
        "Sensitive development detail",
        code="provider_request_rejected",
        status_code=400,
        request_id="provider-request-2",
    )

    data = orchestrator(settings(app_env="production", expose=True))._failure_event_data(
        uuid4(), error
    )

    assert data["code"] == "provider_request_rejected"
    assert "message" not in data
    assert "status_code" not in data
    assert "request_id" not in data
