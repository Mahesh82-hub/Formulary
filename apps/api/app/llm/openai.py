import json
from typing import Any

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


class OpenAIResponsesProvider(HTTPModelProvider):
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
        payload: dict[str, Any] = {
            "model": model,
            "instructions": system_prompt,
            "tools": [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    "strict": False,
                }
                for tool in tools
            ],
            "tool_choice": "auto" if tools else "none",
            "store": False,
        }
        if continuation is None:
            input_items: list[dict[str, Any]] = [message.model_dump() for message in messages]
        else:
            previous_items = continuation.get("input_items")
            if not isinstance(previous_items, list) or not all(
                isinstance(item, dict) for item in previous_items
            ):
                raise LLMProviderError("OpenAI continuation state is invalid")
            input_items = [dict(item) for item in previous_items]
            input_items.extend(
                {
                    "type": "function_call_output",
                    "call_id": output.call_id,
                    "output": json.dumps(output.output, default=str),
                }
                for output in tool_outputs or []
            )
        payload["input"] = input_items

        response = await self.post_json("responses", payload)
        response_id = string_value(response.get("id"), field="id")
        text_parts: list[str] = []
        tool_calls: list[LLMToolCall] = []
        output_items = object_list(response.get("output"), field="output")
        for item in output_items:
            item_type = item.get("type")
            if item_type == "message":
                for content in object_list(item.get("content"), field="message content"):
                    if content.get("type") == "output_text":
                        text_parts.append(string_value(content.get("text"), field="output text"))
            elif item_type == "function_call":
                raw_arguments = string_value(item.get("arguments"), field="tool arguments")
                try:
                    arguments_data: object = json.loads(raw_arguments)
                except json.JSONDecodeError as error:
                    raise LLMProviderError(
                        "OpenAI returned invalid tool arguments",
                        code="provider_invalid_tool_arguments",
                    ) from error
                tool_calls.append(
                    LLMToolCall(
                        id=string_value(item.get("call_id"), field="tool call id"),
                        name=string_value(item.get("name"), field="tool name"),
                        arguments=json_object(arguments_data, field="tool arguments"),
                    )
                )

        usage_value = response.get("usage")
        usage = json_object(usage_value, field="usage") if usage_value is not None else {}
        return LLMCompletion(
            provider_response_id=response_id,
            text="".join(text_parts),
            tool_calls=tool_calls,
            usage=usage,
            continuation={"input_items": [*input_items, *output_items]},
        )
