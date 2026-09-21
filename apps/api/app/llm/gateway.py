from functools import lru_cache
from typing import Literal

from app.core.config import Settings, get_settings
from app.llm.groq import GroqChatProvider
from app.llm.models import (
    LLMCompletion,
    LLMMessage,
    LLMToolDefinition,
    LLMToolOutput,
)
from app.llm.openai import OpenAIResponsesProvider
from app.llm.provider import LLMConfigurationError, LLMProvider

ProviderName = Literal["openai", "groq"]


class LLMGateway:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def validate_selection(self, provider: ProviderName, model: str) -> None:
        if not model.strip():
            raise LLMConfigurationError("No model is configured")
        self._provider(provider)

    async def complete(
        self,
        *,
        provider: ProviderName,
        model: str,
        system_prompt: str,
        messages: list[LLMMessage],
        tools: list[LLMToolDefinition],
        continuation: dict[str, object] | None = None,
        tool_outputs: list[LLMToolOutput] | None = None,
    ) -> LLMCompletion:
        return await self._provider(provider).complete(
            model=model,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            continuation=continuation,
            tool_outputs=tool_outputs,
        )

    def _provider(self, provider: ProviderName) -> LLMProvider:
        if provider == "openai":
            key = self._settings.openai_api_key
            if key is None or not key.get_secret_value():
                raise LLMConfigurationError("OpenAI is not configured")
            return OpenAIResponsesProvider(
                api_key=key.get_secret_value(),
                base_url=self._settings.openai_base_url,
                timeout_seconds=self._settings.llm_request_timeout_seconds,
                max_retries=self._settings.llm_transient_max_retries,
                retry_base_delay_seconds=self._settings.llm_retry_base_delay_seconds,
            )
        key = self._settings.groq_api_key
        if key is None or not key.get_secret_value():
            raise LLMConfigurationError("Groq is not configured")
        return GroqChatProvider(
            api_key=key.get_secret_value(),
            base_url=self._settings.groq_base_url,
            timeout_seconds=self._settings.llm_request_timeout_seconds,
            max_retries=self._settings.llm_transient_max_retries,
            retry_base_delay_seconds=self._settings.llm_retry_base_delay_seconds,
        )


@lru_cache
def get_llm_gateway() -> LLMGateway:
    return LLMGateway(get_settings())
