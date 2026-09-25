import json

import httpx
import pytest

from app.llm.groq import GroqChatProvider
from app.llm.models import LLMMessage, LLMToolDefinition, LLMToolOutput
from app.llm.openai import OpenAIResponsesProvider
from app.llm.provider import LLMProviderError

TOOL = LLMToolDefinition(
    name="convert_mass",
    description="Convert mass units",
    parameters={
        "type": "object",
        "properties": {
            "value": {"type": "number"},
            "from_unit": {"type": "string"},
            "to_unit": {"type": "string"},
        },
        "required": ["value", "from_unit", "to_unit"],
    },
)


@pytest.mark.asyncio
async def test_openai_responses_provider_continues_after_tool_output() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload: dict[str, object] = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "resp_tool",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call_1",
                            "name": "convert_mass",
                            "arguments": '{"value":2500,"from_unit":"mcg","to_unit":"mg"}',
                        }
                    ],
                    "usage": {"input_tokens": 20, "output_tokens": 5},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "resp_final",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "2500 mcg is 2.5 mg."}],
                    }
                ],
                "usage": {"input_tokens": 30, "output_tokens": 10},
            },
        )

    provider = OpenAIResponsesProvider(
        api_key="test",
        base_url="https://api.openai.test/v1",
        timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    first = await provider.complete(
        model="test-model",
        system_prompt="Be careful.",
        messages=[LLMMessage(role="user", content="Convert 2500 mcg to mg")],
        tools=[TOOL],
    )
    second = await provider.complete(
        model="test-model",
        system_prompt="Be careful.",
        messages=[],
        tools=[TOOL],
        continuation=first.continuation,
        tool_outputs=[
            LLMToolOutput(
                call_id="call_1",
                name="convert_mass",
                output={"value": 2.5, "unit": "mg"},
            )
        ],
    )

    assert first.tool_calls[0].name == "convert_mass"
    assert second.text == "2500 mcg is 2.5 mg."
    assert requests[0]["store"] is False
    assert requests[1]["input"] == [
        {"role": "user", "content": "Convert 2500 mcg to mg"},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "convert_mass",
            "arguments": '{"value":2500,"from_unit":"mcg","to_unit":"mg"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": '{"value": 2.5, "unit": "mg"}',
        },
    ]


@pytest.mark.asyncio
async def test_groq_provider_preserves_tool_call_conversation() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload: dict[str, object] = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "groq_tool",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "convert_mass",
                                            "arguments": (
                                                '{"value":2500,"from_unit":"mcg","to_unit":"mg"}'
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 5},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "groq_final",
                "choices": [{"message": {"role": "assistant", "content": "2500 mcg is 2.5 mg."}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 10},
            },
        )

    provider = GroqChatProvider(
        api_key="test",
        base_url="https://api.groq.test/openai/v1",
        timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    first = await provider.complete(
        model="test-model",
        system_prompt="Be careful.",
        messages=[LLMMessage(role="user", content="Convert 2500 mcg to mg")],
        tools=[TOOL],
    )
    second = await provider.complete(
        model="test-model",
        system_prompt="Be careful.",
        messages=[],
        tools=[TOOL],
        continuation=first.continuation,
        tool_outputs=[
            LLMToolOutput(
                call_id="call_1",
                name="convert_mass",
                output={"value": 2.5, "unit": "mg"},
            )
        ],
    )

    assert first.tool_calls[0].arguments["value"] == 2500
    assert second.text == "2500 mcg is 2.5 mg."
    second_messages = requests[1]["messages"]
    assert isinstance(second_messages, list)
    assert second_messages[-1] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"value": 2.5, "unit": "mg"}',
    }


@pytest.mark.asyncio
async def test_groq_provider_updates_system_prompt_and_omits_disabled_tool_fields() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload: dict[str, object] = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "groq_tool_before_synthesis",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_synthesis_1",
                                        "type": "function",
                                        "function": {
                                            "name": "convert_mass",
                                            "arguments": (
                                                '{"value":1000,"from_unit":"mcg","to_unit":"mg"}'
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "groq_synthesis_without_tools",
                "choices": [{"message": {"role": "assistant", "content": "It is 1 mg."}}],
            },
        )

    provider = GroqChatProvider(
        api_key="test",
        base_url="https://api.groq.test/openai/v1",
        timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    first = await provider.complete(
        model="test-model",
        system_prompt="Use tools when needed.",
        messages=[LLMMessage(role="user", content="Convert 1000 mcg")],
        tools=[TOOL],
    )
    second = await provider.complete(
        model="test-model",
        system_prompt="Tool access ended. Synthesize now.",
        messages=[],
        tools=[],
        continuation=first.continuation,
        tool_outputs=[
            LLMToolOutput(
                call_id="call_synthesis_1",
                name="convert_mass",
                output={"value": 1.0, "unit": "mg"},
            )
        ],
    )

    assert second.text == "It is 1 mg."
    assert "tools" not in requests[1]
    assert "tool_choice" not in requests[1]
    second_messages = requests[1]["messages"]
    assert isinstance(second_messages, list)
    assert second_messages[0] == {
        "role": "system",
        "content": "Tool access ended. Synthesize now.",
    }


@pytest.mark.asyncio
async def test_groq_provider_retries_transient_http_failure_once() -> None:
    request_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return httpx.Response(
                503,
                headers={"x-request-id": "groq-temporary-1"},
                json={"error": {"message": "Service temporarily unavailable"}},
            )
        return httpx.Response(
            200,
            json={
                "id": "groq_recovered",
                "choices": [{"message": {"role": "assistant", "content": "Recovered."}}],
            },
        )

    provider = GroqChatProvider(
        api_key="test",
        base_url="https://api.groq.test/openai/v1",
        timeout_seconds=10,
        max_retries=1,
        retry_base_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    )

    completion = await provider.complete(
        model="test-model",
        system_prompt="Be careful.",
        messages=[LLMMessage(role="user", content="Try once")],
        tools=[],
    )

    assert completion.text == "Recovered."
    assert request_count == 2


@pytest.mark.asyncio
async def test_groq_provider_does_not_retry_nontransient_http_failure() -> None:
    request_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            400,
            headers={"x-request-id": "groq-bad-request-1"},
            json={
                "error": {
                    "message": "Tool arguments were rejected",
                    "type": "invalid_request_error",
                    "code": "tool_use_failed",
                }
            },
        )

    provider = GroqChatProvider(
        api_key="test",
        base_url="https://api.groq.test/openai/v1",
        timeout_seconds=10,
        max_retries=1,
        retry_base_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMProviderError) as captured:
        await provider.complete(
            model="test-model",
            system_prompt="Be careful.",
            messages=[LLMMessage(role="user", content="Use a tool")],
            tools=[TOOL],
        )

    assert request_count == 1
    assert captured.value.code == "provider_request_rejected"
    assert captured.value.status_code == 400
    assert captured.value.request_id == "groq-bad-request-1"
    assert captured.value.retryable is False
    assert captured.value.details == {
        "provider_message": "Tool arguments were rejected",
        "provider_error_type": "invalid_request_error",
        "provider_error_code": "tool_use_failed",
    }


@pytest.mark.asyncio
async def test_rate_limits_wait_as_long_as_the_provider_asks_then_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Groq returned 'try again in 894ms'; a fixed 0.25 s retry simply failed again."""
    import asyncio

    from app.llm.groq import GroqChatProvider
    from app.llm.models import LLMMessage

    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", record)
    responses = [
        httpx.Response(
            429,
            json={"error": {"message": "Rate limit reached. Please try again in 894.93ms."}},
        ),
        httpx.Response(429, headers={"Retry-After": "2"}, json={"error": {"message": "slow"}}),
        httpx.Response(
            200,
            json={
                "id": "ok",
                "choices": [{"message": {"role": "assistant", "content": "Done."}}],
            },
        ),
    ]
    provider = GroqChatProvider(
        api_key="key",
        base_url="https://groq.test/openai/v1",
        timeout_seconds=10,
        max_retries=0,
        transport=httpx.MockTransport(lambda _: responses.pop(0)),
    )

    completion = await provider.complete(
        model="m", system_prompt="s", messages=[LLMMessage(role="user", content="hi")], tools=[]
    )

    assert completion.text == "Done."
    assert 0.9 < waits[0] < 1.2  # the 894 ms the provider asked for, plus headroom
    assert 2.0 < waits[1] < 2.5  # Retry-After: 2
