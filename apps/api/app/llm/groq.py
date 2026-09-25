import json
import logging
from typing import Any, cast

import httpx

from app.llm.http import HTTPModelProvider, json_object, object_list, string_value
from app.llm.models import (
    LLMCompletion,
    LLMMessage,
    LLMToolCall,
    LLMToolDefinition,
    LLMToolOutput,
)
from app.llm.provider import LLMProviderError
from app.llm.sanitize import strip_protocol_leaks
from app.llm.web_citations import page_excerpts, parse_executed_tools, rewrite_citations

logger = logging.getLogger(__name__)


class GroqChatProvider(HTTPModelProvider):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout_seconds: float,
        max_retries: int = 1,
        retry_base_delay_seconds: float = 0.25,
        max_rate_limit_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
        web_search_models: frozenset[str] = frozenset(),
    ) -> None:
        # Models that get Groq's built-in browser_search next to the application's tools.
        self._web_search_models = web_search_models
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            retry_base_delay_seconds=retry_base_delay_seconds,
            max_rate_limit_retries=max_rate_limit_retries,
            transport=transport,
        )

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
    ) -> LLMCompletion:
        conversation = self._conversation(system_prompt, messages, continuation)
        conversation.extend(
            {
                "role": "tool",
                "tool_call_id": output.call_id,
                "content": json.dumps(output.output, default=str),
            }
            for output in tool_outputs or []
        )
        payload: dict[str, Any] = {
            "model": model,
            "messages": conversation,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]
            # Built-in web search is offered only while tools are allowed. Synthesis turns pass
            # no tools, and must not start new research through the back door.
            if allow_web_search and model in self._web_search_models:
                payload["tools"].append({"type": "browser_search"})
            payload["tool_choice"] = "auto"
        response = await self.post_json("chat/completions", payload)
        response_id = string_value(response.get("id"), field="id")
        choices = object_list(response.get("choices"), field="choices")
        if not choices:
            raise LLMProviderError(
                "Groq returned no completion choices",
                code="provider_invalid_response",
            )
        message = json_object(choices[0].get("message"), field="choice message")
        text_value = message.get("content")
        text = text_value if isinstance(text_value, str) else ""
        text, leaked = strip_protocol_leaks(text)
        if leaked:
            logger.warning("Removed leaked chat-protocol fragment from Groq response %s", model)
        web_queries, web_pages = parse_executed_tools(message.get("executed_tools"))
        web_excerpts = page_excerpts(message.get("executed_tools"))
        text, web_sources = rewrite_citations(text, web_pages)
        if not web_sources and web_pages:
            # Pages were read but none cited inline; they still informed the answer.
            web_sources = list(web_pages.values())

        raw_tool_calls = message.get("tool_calls")
        tool_call_items = (
            object_list(raw_tool_calls, field="tool calls") if raw_tool_calls is not None else []
        )
        tool_calls: list[LLMToolCall] = []
        continuation_tool_calls: list[dict[str, Any]] = []
        for item in tool_call_items:
            function = json_object(item.get("function"), field="tool function")
            raw_arguments = string_value(function.get("arguments"), field="tool arguments")
            try:
                arguments_data: object = json.loads(raw_arguments)
            except json.JSONDecodeError as error:
                raise LLMProviderError(
                    "Groq returned invalid tool arguments",
                    code="provider_invalid_tool_arguments",
                ) from error
            call = LLMToolCall(
                id=string_value(item.get("id"), field="tool call id"),
                name=string_value(function.get("name"), field="tool name"),
                arguments=json_object(arguments_data, field="tool arguments"),
            )
            tool_calls.append(call)
            continuation_tool_calls.append(item)

        assistant_message: dict[str, Any] = {"role": "assistant", "content": text or None}
        if continuation_tool_calls:
            assistant_message["tool_calls"] = continuation_tool_calls
        conversation.append(assistant_message)
        usage_value = response.get("usage")
        usage = json_object(usage_value, field="usage") if usage_value is not None else {}
        return LLMCompletion(
            provider_response_id=response_id,
            text=text,
            tool_calls=tool_calls,
            usage=usage,
            continuation={"messages": conversation},
            web_queries=web_queries,
            web_sources=web_sources,
            web_excerpts=web_excerpts,
        )

    @staticmethod
    def _conversation(
        system_prompt: str,
        messages: list[LLMMessage],
        continuation: dict[str, object] | None,
    ) -> list[dict[str, Any]]:
        if continuation is None:
            return [
                {"role": "system", "content": system_prompt},
                *(message.model_dump() for message in messages),
            ]
        raw_messages = continuation.get("messages")
        if not isinstance(raw_messages, list) or not all(
            isinstance(message, dict) for message in raw_messages
        ):
            raise LLMProviderError("Groq continuation state is invalid")
        conversation = [cast(dict[str, Any], message).copy() for message in raw_messages]
        if conversation and conversation[0].get("role") == "system":
            conversation[0] = {"role": "system", "content": system_prompt}
        else:
            conversation.insert(0, {"role": "system", "content": system_prompt})
        return conversation
