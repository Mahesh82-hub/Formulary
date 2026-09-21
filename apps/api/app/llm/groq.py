import json
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


class GroqChatProvider(HTTPModelProvider):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout_seconds: float,
        max_retries: int = 1,
        retry_base_delay_seconds: float = 0.25,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            retry_base_delay_seconds=retry_base_delay_seconds,
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
