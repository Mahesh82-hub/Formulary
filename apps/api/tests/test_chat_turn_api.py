import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.api.dependencies.auth import get_current_user
from app.core.config import get_settings
from app.db.session import async_session_factory
from app.llm.gateway import get_llm_gateway
from app.llm.models import LLMCompletion, LLMToolCall
from app.llm.provider import LLMProviderError
from app.main import app
from app.mcp_gateway.client import get_mcp_tool_client
from app.models import AssistantRun, Conversation, Message, ToolExecution, User


class ScriptedToolCallingGateway:
    def __init__(self) -> None:
        self.call_count = 0

    def validate_selection(self, provider: str, model: str) -> None:
        assert provider == "groq"
        assert model == "test-model"

    async def complete(self, **_: Any) -> LLMCompletion:
        self.call_count += 1
        if self.call_count == 1:
            return LLMCompletion(
                provider_response_id="provider-tool-response",
                tool_calls=[
                    LLMToolCall(
                        id="call-1",
                        name="convert_mass",
                        arguments={"value": 2500, "from_unit": "mcg", "to_unit": "mg"},
                    )
                ],
                usage={"input_tokens": 20, "output_tokens": 5},
                continuation={"test": "continuation"},
            )
        return LLMCompletion(
            provider_response_id="provider-final-response",
            text="2500 micrograms equals 2.5 milligrams.",
            usage={"input_tokens": 30, "output_tokens": 10},
        )


class ScriptedClarificationGateway:
    def __init__(self) -> None:
        self.call_count = 0

    def validate_selection(self, provider: str, model: str) -> None:
        assert provider == "groq"
        assert model == "test-model"

    async def complete(self, **kwargs: Any) -> LLMCompletion:
        self.call_count += 1
        if self.call_count == 1:
            tool_names = {tool.name for tool in kwargs["tools"]}
            assert "prepare_bioequivalence_evidence_request" in tool_names
            assert "call prepare_bioequivalence_evidence_request before" in kwargs["system_prompt"]
            return LLMCompletion(
                provider_response_id="provider-intake-response",
                tool_calls=[
                    LLMToolCall(
                        id="intake-call-1",
                        name="prepare_bioequivalence_evidence_request",
                        arguments={"drug_name": "metformin"},
                    )
                ],
                usage={"input_tokens": 25, "output_tokens": 5},
                continuation={"test": "intake-continuation"},
            )

        tool_outputs = kwargs["tool_outputs"]
        assert len(tool_outputs) == 1
        intake = tool_outputs[0].output
        assert intake["status"] == "needs_clarification"
        assert intake["recommended_next_action"] == "ask_user"
        assert len(intake["questions"]) == 2
        return LLMCompletion(
            provider_response_id="provider-clarification-response",
            text=(
                "Before I research the evidence, I need two details:\n\n"
                "1. Do you want source-reported FDA reference values or a bioequivalence "
                "comparison with company data?\n"
                "2. What dosage form, route, and strength or dose should I target?"
            ),
            usage={"input_tokens": 35, "output_tokens": 25},
        )


class ScriptedFailingGateway:
    def validate_selection(self, provider: str, model: str) -> None:
        assert provider == "groq"
        assert model == "test-model"

    async def complete(self, **_: Any) -> LLMCompletion:
        raise LLMProviderError(
            "The model provider is temporarily unavailable (HTTP 503)",
            code="provider_unavailable",
            retryable=True,
            status_code=503,
            request_id="groq-integration-request",
            retry_count=1,
            details={"provider_error_code": "service_unavailable"},
        )


class ScriptedBudgetGateway:
    def __init__(self) -> None:
        self.call_count = 0

    def validate_selection(self, provider: str, model: str) -> None:
        assert provider == "groq"
        assert model == "test-model"

    async def complete(self, **kwargs: Any) -> LLMCompletion:
        self.call_count += 1
        if self.call_count == 1:
            assert kwargs["tools"]
            return LLMCompletion(
                provider_response_id="provider-budget-tool-response",
                tool_calls=[
                    LLMToolCall(
                        id="budget-call-1",
                        name="convert_mass",
                        arguments={"value": 1000, "from_unit": "mcg", "to_unit": "mg"},
                    )
                ],
                continuation={"test": "budget-continuation"},
            )

        if self.call_count == 2:
            assert kwargs["tools"] == []
            assert "Tool access has now ended" in kwargs["system_prompt"]
            assert kwargs["tool_outputs"][0].output == {"value": 1.0, "unit": "mg"}
            raise LLMProviderError(
                "Tool choice is none, but model called a tool",
                code="provider_request_rejected",
                status_code=400,
                details={"provider_error_code": "tool_use_failed"},
            )

        assert kwargs["tools"] == []
        assert kwargs["continuation"] is None
        assert kwargs["tool_outputs"] is None
        assert '"value": 1.0' in kwargs["messages"][-1].content
        return LLMCompletion(
            provider_response_id="provider-budget-synthesis-response",
            text=(
                "The collected result is 1 mg. If you want, ask me to continue researching "
                "related conversion evidence in your next message."
            ),
        )


class ScriptedDuplicateToolGateway:
    def __init__(self) -> None:
        self.call_count = 0

    def validate_selection(self, provider: str, model: str) -> None:
        assert provider == "groq"
        assert model == "test-model"

    async def complete(self, **kwargs: Any) -> LLMCompletion:
        self.call_count += 1
        if self.call_count <= 2:
            if self.call_count == 2:
                assert kwargs["tool_outputs"][0].output == {"value": 2.0, "unit": "mg"}
            return LLMCompletion(
                provider_response_id=f"provider-duplicate-response-{self.call_count}",
                tool_calls=[
                    LLMToolCall(
                        id=f"duplicate-call-{self.call_count}",
                        name="convert_mass",
                        arguments={"value": 2000, "from_unit": "mcg", "to_unit": "mg"},
                    )
                ],
                continuation={"test": f"duplicate-continuation-{self.call_count}"},
            )

        duplicate_output = kwargs["tool_outputs"][0]
        assert duplicate_output.is_error is True
        assert duplicate_output.output["reason"] == "duplicate_tool_call"
        return LLMCompletion(
            provider_response_id="provider-duplicate-final-response",
            text="The result is 2 mg; the repeated lookup was not needed.",
        )


class ScriptedToolValidationFailureGateway:
    def __init__(self) -> None:
        self.call_count = 0

    def validate_selection(self, provider: str, model: str) -> None:
        assert provider == "groq"
        assert model == "test-model"

    async def complete(self, **kwargs: Any) -> LLMCompletion:
        self.call_count += 1
        if self.call_count == 1:
            raise LLMProviderError(
                "Tool call validation failed",
                code="provider_request_rejected",
                status_code=400,
                details={"provider_error_code": "tool_use_failed"},
            )
        assert kwargs["tools"] == []
        assert "provider rejected a proposed tool call" in kwargs["system_prompt"]
        return LLMCompletion(
            provider_response_id="provider-validation-recovery-response",
            text=(
                "I could not run the proposed lookup, so this answer is limited to the evidence "
                "already available. Ask me to continue with a revised search if needed."
            ),
        )


class ScriptedSequentialClarificationGateway:
    def __init__(self) -> None:
        self.call_count = 0

    def validate_selection(self, provider: str, model: str) -> None:
        assert provider == "groq"
        assert model == "test-model"

    async def complete(self, **kwargs: Any) -> LLMCompletion:
        self.call_count += 1
        tool_names = {tool.name for tool in kwargs["tools"]}
        assert "request_user_clarification" in tool_names
        if self.call_count == 1:
            return LLMCompletion(
                provider_response_id="provider-general-question-1",
                tool_calls=[
                    LLMToolCall(
                        id="general-question-call-1",
                        name="request_user_clarification",
                        arguments={
                            "title": "Choose the FDA scope",
                            "question": "Which FDA information should I focus on?",
                            "suggestions": ["Current labeling", "Approval history"],
                            "reason": "The answer changes which FDA dataset should be searched.",
                        },
                    )
                ],
                continuation={"test": "general-question-1"},
            )
        assert any(
            "[STRUCTURED CLARIFICATION REQUEST]" in message.content
            for message in kwargs["messages"]
        )
        if self.call_count == 2:
            assert any("Current labeling" in message.content for message in kwargs["messages"])
            return LLMCompletion(
                provider_response_id="provider-general-question-2",
                tool_calls=[
                    LLMToolCall(
                        id="general-question-call-2",
                        name="request_user_clarification",
                        arguments={
                            "title": "Choose the product",
                            "question": "Should I research a brand or the active ingredient?",
                            "suggestions": ["Brand product", "Active ingredient"],
                        },
                    )
                ],
                continuation={"test": "general-question-2"},
            )
        assert any("Active ingredient" in message.content for message in kwargs["messages"])
        return LLMCompletion(
            provider_response_id="provider-general-final",
            text="I now have enough context to answer the normal FDA chat question.",
        )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_chat_turn_streams_mcp_tool_and_persists_assistant_run() -> None:
    async with async_session_factory() as session:
        user = User(email=f"turn-{uuid4()}@example.com")
        session.add(user)
        await session.commit()
        await session.refresh(user)

    gateway = ScriptedToolCallingGateway()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_llm_gateway] = lambda: gateway
    conversation_id: str | None = None
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            conversation_response = await client.post("/api/v1/conversations", json={})
            assert conversation_response.status_code == 201
            conversation_id = conversation_response.json()["id"]

            turn_response = await client.post(
                f"/api/v1/conversations/{conversation_id}/turns",
                json={
                    "text": "Convert 2500 mcg to mg",
                    "provider": "groq",
                    "model": "test-model",
                },
            )

            assert turn_response.status_code == 200
            assert turn_response.headers["content-type"].startswith("text/event-stream")
            stream = turn_response.text
            assert "event: run.started" in stream
            assert "event: tool.started" in stream
            assert "event: tool.completed" in stream
            assert "event: message.delta" in stream
            assert "event: message.completed" in stream
            assert "2500 micrograms equals 2.5 milligrams." in stream

            detail_response = await client.get(f"/api/v1/conversations/{conversation_id}")
            first_assistant_id = detail_response.json()["messages"][-1]["id"]
            regenerate_response = await client.post(
                f"/api/v1/conversations/{conversation_id}/messages/{first_assistant_id}/regenerate",
                json={"provider": "groq", "model": "test-model"},
            )
            assert regenerate_response.status_code == 200
            assert "event: message.completed" in regenerate_response.text

        async with async_session_factory() as session:
            conversation = await session.scalar(
                select(Conversation).where(Conversation.id == conversation_id)
            )
            assert conversation is not None
            assert conversation.title == "Convert 2500 mcg to mg"

            messages = (
                await session.scalars(
                    select(Message)
                    .where(Message.conversation_id == conversation.id)
                    .order_by(Message.created_at)
                )
            ).all()
            assert [message.role for message in messages] == ["user", "assistant", "assistant"]
            assert messages[-1].plain_text == "2500 micrograms equals 2.5 milligrams."
            assert messages[-1].supersedes_message_id == messages[-2].id
            assert conversation.active_leaf_message_id == messages[-1].id

            runs = (
                await session.scalars(
                    select(AssistantRun)
                    .where(AssistantRun.conversation_id == conversation.id)
                    .order_by(AssistantRun.created_at)
                )
            ).all()
            assert [run.status for run in runs] == ["completed", "completed"]
            assert runs[-1].response_message_id == messages[-1].id
            assert runs[-1].retry_of_run_id == runs[0].id
            assert len(runs[0].usage["rounds"]) == 2

            execution = await session.scalar(
                select(ToolExecution).where(ToolExecution.run_id == runs[0].id)
            )
            assert execution is not None
            assert execution.status == "completed"
            assert execution.tool_name == "convert_mass"
            assert execution.result is not None
            assert execution.result["data"] == {"value": 2.5, "unit": "mg"}
            assert execution.result["audit"] == {
                "evidence_chunk_ids": [],
                "result_character_count": 28,
                "returned_to_llm": True,
            }
            assert runs[0].orchestration_state["prompt_received"] is True
            assert runs[0].orchestration_state["prompt_character_count"] == 22
            assert runs[0].orchestration_state["response_stored_in_postgres"] is True
            assert gateway.call_count == 3
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_llm_gateway, None)
        async with async_session_factory() as session:
            await session.execute(delete(User).where(User.id == user.id))
            await session.commit()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_chat_turn_synthesizes_when_research_budget_is_reached() -> None:
    async with async_session_factory() as session:
        user = User(email=f"budget-{uuid4()}@example.com")
        session.add(user)
        await session.commit()
        await session.refresh(user)

    configuration = get_settings()
    previous_round_limit = configuration.chat_max_tool_rounds
    previous_call_limit = configuration.chat_max_tool_calls
    configuration.chat_max_tool_rounds = 1
    configuration.chat_max_tool_calls = 12
    gateway = ScriptedBudgetGateway()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_llm_gateway] = lambda: gateway
    conversation_id: str | None = None
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            conversation_response = await client.post("/api/v1/conversations", json={})
            assert conversation_response.status_code == 201
            conversation_id = conversation_response.json()["id"]

            turn_response = await client.post(
                f"/api/v1/conversations/{conversation_id}/turns",
                json={
                    "text": "Research this conversion with a deliberately short budget",
                    "provider": "groq",
                    "model": "test-model",
                },
            )

            assert turn_response.status_code == 200
            assert "event: run.failed" not in turn_response.text
            assert "event: message.completed" in turn_response.text
            assert "ask me to continue researching" in turn_response.text

        async with async_session_factory() as session:
            run = await session.scalar(
                select(AssistantRun).where(AssistantRun.conversation_id == conversation_id)
            )
            assert run is not None
            assert run.status == "completed"
            assert run.orchestration_state["completion_reason"] == "research_budget_reached"
            assert run.orchestration_state["research_limit_reason"] == "tool_round_budget"
            assert run.orchestration_state["tool_calls_executed"] == 1
            assert run.orchestration_state["clean_synthesis_fallback_used"] is True
            assert run.orchestration_state["user_may_continue_research"] is True
            assert gateway.call_count == 3
    finally:
        configuration.chat_max_tool_rounds = previous_round_limit
        configuration.chat_max_tool_calls = previous_call_limit
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_llm_gateway, None)
        async with async_session_factory() as session:
            await session.execute(delete(User).where(User.id == user.id))
            await session.commit()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_chat_turn_suppresses_exact_duplicate_tool_calls() -> None:
    async with async_session_factory() as session:
        user = User(email=f"duplicate-tool-{uuid4()}@example.com")
        session.add(user)
        await session.commit()
        await session.refresh(user)

    gateway = ScriptedDuplicateToolGateway()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_llm_gateway] = lambda: gateway
    conversation_id: str | None = None
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            conversation_response = await client.post("/api/v1/conversations", json={})
            assert conversation_response.status_code == 201
            conversation_id = conversation_response.json()["id"]

            turn_response = await client.post(
                f"/api/v1/conversations/{conversation_id}/turns",
                json={
                    "text": "Convert 2000 mcg to mg without repeating work",
                    "provider": "groq",
                    "model": "test-model",
                },
            )

            assert turn_response.status_code == 200
            assert "event: run.failed" not in turn_response.text
            assert "The result is 2 mg" in turn_response.text

        async with async_session_factory() as session:
            run = await session.scalar(
                select(AssistantRun).where(AssistantRun.conversation_id == conversation_id)
            )
            assert run is not None
            executions = (
                await session.scalars(
                    select(ToolExecution)
                    .where(ToolExecution.run_id == run.id)
                    .order_by(ToolExecution.created_at)
                )
            ).all()
            assert len(executions) == 2
            assert executions[0].result is not None
            assert executions[0].result["data"] == {"value": 2.0, "unit": "mg"}
            assert executions[1].result is not None
            assert executions[1].result["audit"]["execution_suppressed"] is True
            assert executions[1].result["audit"]["suppression_reason"] == "duplicate_tool_call"
            assert run.orchestration_state["tool_calls_executed"] == 1
            assert run.orchestration_state["tool_calls_suppressed"] == 1
            assert gateway.call_count == 3
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_llm_gateway, None)
        async with async_session_factory() as session:
            await session.execute(delete(User).where(User.id == user.id))
            await session.commit()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_chat_turn_recovers_from_provider_tool_validation_failure() -> None:
    async with async_session_factory() as session:
        user = User(email=f"tool-validation-{uuid4()}@example.com")
        session.add(user)
        await session.commit()
        await session.refresh(user)

    gateway = ScriptedToolValidationFailureGateway()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_llm_gateway] = lambda: gateway
    conversation_id: str | None = None
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            conversation_response = await client.post("/api/v1/conversations", json={})
            assert conversation_response.status_code == 201
            conversation_id = conversation_response.json()["id"]

            turn_response = await client.post(
                f"/api/v1/conversations/{conversation_id}/turns",
                json={
                    "text": "Research a label section the model may format incorrectly",
                    "provider": "groq",
                    "model": "test-model",
                },
            )

            assert turn_response.status_code == 200
            assert "event: run.failed" not in turn_response.text
            assert "event: message.completed" in turn_response.text
            assert "answer is limited" in turn_response.text

        async with async_session_factory() as session:
            run = await session.scalar(
                select(AssistantRun).where(AssistantRun.conversation_id == conversation_id)
            )
            assert run is not None
            assert run.status == "completed"
            assert run.orchestration_state["completion_reason"] == "tool_validation_recovered"
            assert run.orchestration_state["tool_validation_recovered"] is True
            assert run.orchestration_state["user_may_continue_research"] is True
            assert gateway.call_count == 2
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_llm_gateway, None)
        async with async_session_factory() as session:
            await session.execute(delete(User).where(User.id == user.id))
            await session.commit()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_chat_turn_persists_safe_provider_failure_diagnostics() -> None:
    async with async_session_factory() as session:
        user = User(email=f"provider-failure-{uuid4()}@example.com")
        session.add(user)
        await session.commit()
        await session.refresh(user)

    gateway = ScriptedFailingGateway()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_llm_gateway] = lambda: gateway
    conversation_id: str | None = None
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            conversation_response = await client.post("/api/v1/conversations", json={})
            assert conversation_response.status_code == 201
            conversation_id = conversation_response.json()["id"]

            turn_response = await client.post(
                f"/api/v1/conversations/{conversation_id}/turns",
                json={
                    "text": "Research metformin",
                    "provider": "groq",
                    "model": "test-model",
                },
            )

            assert turn_response.status_code == 200
            assert "event: run.failed" in turn_response.text
            assert '"code": "provider_unavailable"' in turn_response.text

        async with async_session_factory() as session:
            run = await session.scalar(
                select(AssistantRun).where(AssistantRun.conversation_id == conversation_id)
            )
            assert run is not None
            assert run.status == "failed"
            assert run.error == {
                "code": "provider_unavailable",
                "message": "The model provider is temporarily unavailable (HTTP 503)",
                "retryable": True,
                "retry_count": 1,
                "status_code": 503,
                "request_id": "groq-integration-request",
                "details": {"provider_error_code": "service_unavailable"},
            }
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_llm_gateway, None)
        async with async_session_factory() as session:
            await session.execute(delete(User).where(User.id == user.id))
            await session.commit()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_chat_turn_asks_for_simulation_context_before_research() -> None:
    async with async_session_factory() as session:
        user = User(email=f"intake-{uuid4()}@example.com")
        session.add(user)
        await session.commit()
        await session.refresh(user)

    gateway = ScriptedClarificationGateway()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_llm_gateway] = lambda: gateway
    conversation_id: str | None = None
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            conversation_response = await client.post("/api/v1/conversations", json={})
            assert conversation_response.status_code == 201
            conversation_id = conversation_response.json()["id"]

            turn_response = await client.post(
                f"/api/v1/conversations/{conversation_id}/turns",
                json={
                    "text": "Give me metformin values for a digital-human simulation",
                    "provider": "groq",
                    "model": "test-model",
                },
            )

            assert turn_response.status_code == 200
            assert "event: tool.completed" in turn_response.text
            assert "I need a few details before I continue" in turn_response.text
            assert "event: message.completed" in turn_response.text

        async with async_session_factory() as session:
            executions = (
                await session.scalars(
                    select(ToolExecution)
                    .join(AssistantRun, ToolExecution.run_id == AssistantRun.id)
                    .where(AssistantRun.conversation_id == conversation_id)
                )
            ).all()
            assert [execution.tool_name for execution in executions] == [
                "prepare_bioequivalence_evidence_request"
            ]
            assert executions[0].result is not None
            assert executions[0].result["data"]["status"] == "needs_clarification"
            assistant_message = await session.scalar(
                select(Message).where(
                    Message.conversation_id == conversation_id,
                    Message.role == "assistant",
                )
            )
            assert assistant_message is not None
            clarification = next(
                block for block in assistant_message.content if block.get("type") == "clarification"
            )
            assert clarification["status"] == "awaiting_user"
            assert clarification["questions"][0]["question_id"] == "research_objective"
            run = await session.scalar(
                select(AssistantRun).where(AssistantRun.conversation_id == conversation_id)
            )
            assert run is not None
            assert run.orchestration_state["completion_reason"] == "awaiting_clarification"
            assert run.orchestration_state["awaiting_user_input"] is True
            assert gateway.call_count == 1
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_llm_gateway, None)
        async with async_session_factory() as session:
            await session.execute(delete(User).where(User.id == user.id))
            await session.commit()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_normal_chat_supports_sequential_interactive_questions() -> None:
    async with async_session_factory() as session:
        user = User(email=f"general-clarification-{uuid4()}@example.com")
        session.add(user)
        await session.commit()
        await session.refresh(user)

    gateway = ScriptedSequentialClarificationGateway()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_llm_gateway] = lambda: gateway
    conversation_id: str | None = None
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            conversation_response = await client.post("/api/v1/conversations", json={})
            assert conversation_response.status_code == 201
            conversation_id = conversation_response.json()["id"]

            first_turn = await client.post(
                f"/api/v1/conversations/{conversation_id}/turns",
                json={
                    "text": "Tell me about this drug using FDA information",
                    "provider": "groq",
                    "model": "test-model",
                },
            )
            assert first_turn.status_code == 200
            assert "I need one detail before I continue" in first_turn.text

            first_detail = await client.get(f"/api/v1/conversations/{conversation_id}")
            first_block = first_detail.json()["messages"][-1]["content"][1]
            assert first_block["task_type"] == "general_chat"
            assert first_block["questions"][0]["suggestions"] == [
                "Current labeling",
                "Approval history",
            ]

            second_turn = await client.post(
                f"/api/v1/conversations/{conversation_id}/turns",
                json={
                    "text": (
                        "Clarification response for the ongoing research task:\n"
                        "- general_clarification: Current labeling\n"
                        "Please continue the original task using these details."
                    ),
                    "provider": "groq",
                    "model": "test-model",
                },
            )
            assert second_turn.status_code == 200
            second_detail = await client.get(f"/api/v1/conversations/{conversation_id}")
            second_block = second_detail.json()["messages"][-1]["content"][1]
            assert second_block["questions"][0]["prompt"] == (
                "Should I research a brand or the active ingredient?"
            )

            final_turn = await client.post(
                f"/api/v1/conversations/{conversation_id}/turns",
                json={
                    "text": (
                        "Clarification response for the ongoing research task:\n"
                        "- general_clarification: Active ingredient\n"
                        "Please continue the original task using these details."
                    ),
                    "provider": "groq",
                    "model": "test-model",
                },
            )
            assert final_turn.status_code == 200
            assert "I now have enough context" in final_turn.text

        async with async_session_factory() as session:
            runs = (
                await session.scalars(
                    select(AssistantRun)
                    .where(AssistantRun.conversation_id == conversation_id)
                    .order_by(AssistantRun.created_at)
                )
            ).all()
            assert [run.orchestration_state["completion_reason"] for run in runs] == [
                "awaiting_clarification",
                "awaiting_clarification",
                "model_response",
            ]
            assert gateway.call_count == 3
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_llm_gateway, None)
        async with async_session_factory() as session:
            await session.execute(delete(User).where(User.id == user.id))
            await session.commit()


class ScriptedParallelToolGateway:
    """Requests three tools in one turn, the way providers actually batch them."""

    def __init__(self) -> None:
        self.call_count = 0

    def validate_selection(self, provider: str, model: str) -> None: ...

    async def complete(self, **_: Any) -> LLMCompletion:
        self.call_count += 1
        if self.call_count == 1:
            return LLMCompletion(
                provider_response_id="provider-parallel",
                tool_calls=[
                    LLMToolCall(
                        id=f"parallel-call-{index}",
                        name="convert_mass",
                        arguments={"value": index * 1000, "from_unit": "mcg", "to_unit": "mg"},
                    )
                    for index in range(1, 4)
                ],
                continuation={"test": "parallel"},
            )
        return LLMCompletion(
            provider_response_id="provider-parallel-final",
            text="All three conversions are complete.",
        )


class DelayingToolClient:
    """Delegates to the real tool client, adding latency and recording overlap."""

    def __init__(self, inner: Any, delay: float) -> None:
        self._inner = inner
        self._delay = delay
        self._active = 0
        self.concurrent_peak = 0

    async def list_tools(self) -> Any:
        return await self._inner.list_tools()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self._active += 1
        self.concurrent_peak = max(self.concurrent_peak, self._active)
        try:
            await asyncio.sleep(self._delay)
            return await self._inner.call_tool(name, arguments)
        finally:
            self._active -= 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_tool_calls_in_one_turn_execute_concurrently() -> None:
    """Several tools requested in one turn must overlap rather than queue."""
    async with async_session_factory() as session:
        user = User(email=f"parallel-{uuid4()}@example.com")
        session.add(user)
        await session.commit()
        await session.refresh(user)

    delay = 0.25
    delaying = DelayingToolClient(get_mcp_tool_client(), delay)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_llm_gateway] = lambda: ScriptedParallelToolGateway()
    app.dependency_overrides[get_mcp_tool_client] = lambda: delaying
    conversation_id: str | None = None
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            created = await client.post("/api/v1/conversations", json={})
            conversation_id = created.json()["id"]

            started = asyncio.get_running_loop().time()
            response = await client.post(
                f"/api/v1/conversations/{conversation_id}/turns",
                json={
                    "text": "Convert three values",
                    "provider": "groq",
                    "model": "test-model",
                },
            )
            body = response.text
            elapsed = asyncio.get_running_loop().time() - started

        assert response.status_code == 200
        assert body.count("event: tool.completed") == 3
    finally:
        app.dependency_overrides.clear()
        if conversation_id is not None:
            async with async_session_factory() as session:
                await session.execute(
                    delete(Conversation).where(Conversation.id == UUID(conversation_id))
                )
                await session.execute(delete(User).where(User.id == user.id))
                await session.commit()

    assert delaying.concurrent_peak == 3, "tool calls did not overlap"
    # Sequential execution would cost at least three delays.
    assert elapsed < delay * 2.5


class ScriptedEmptyFinalResponseGateway:
    """Researches, then returns an empty message - the intermittent GPT-OSS failure."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def validate_selection(self, provider: str, model: str) -> None: ...

    async def complete(self, **kwargs: Any) -> LLMCompletion:
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return LLMCompletion(
                provider_response_id="empty-1",
                tool_calls=[
                    LLMToolCall(
                        id="empty-call",
                        name="convert_mass",
                        arguments={"value": 2500, "from_unit": "mcg", "to_unit": "mg"},
                    )
                ],
                continuation={"test": "empty"},
            )
        if kwargs.get("tools"):
            return LLMCompletion(provider_response_id="empty-2", text="")
        return LLMCompletion(
            provider_response_id="empty-synthesis", text="2500 micrograms is 2.5 milligrams."
        )


class ScriptedRepeatingToolGateway:
    """Keeps calling one broad tool with reworded arguments."""

    def __init__(self) -> None:
        self.call_count = 0

    def validate_selection(self, provider: str, model: str) -> None: ...

    async def complete(self, **_: Any) -> LLMCompletion:
        self.call_count += 1
        if self.call_count == 1:
            return LLMCompletion(
                provider_response_id="repeat-1",
                tool_calls=[
                    LLMToolCall(
                        id=f"repeat-{index}",
                        name="convert_mass",
                        arguments={"value": 1000 * index, "from_unit": "mcg", "to_unit": "mg"},
                    )
                    for index in range(1, 5)
                ],
                continuation={"test": "repeat"},
            )
        return LLMCompletion(provider_response_id="repeat-final", text="Converted the values.")


async def _turn(gateway: Any, text: str) -> tuple[str, AssistantRun, list[ToolExecution]]:
    async with async_session_factory() as session:
        user = User(email=f"resilience-{uuid4()}@example.com")
        session.add(user)
        await session.commit()
        await session.refresh(user)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_llm_gateway] = lambda: gateway
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            created = await client.post("/api/v1/conversations", json={})
            conversation_id = created.json()["id"]
            response = await client.post(
                f"/api/v1/conversations/{conversation_id}/turns",
                json={"text": text, "provider": "groq", "model": "test-model"},
            )
        async with async_session_factory() as session:
            run = await session.scalar(
                select(AssistantRun).where(AssistantRun.conversation_id == UUID(conversation_id))
            )
            assert run is not None
            executions = list(
                await session.scalars(select(ToolExecution).where(ToolExecution.run_id == run.id))
            )
        return response.text, run, executions
    finally:
        app.dependency_overrides.clear()
        async with async_session_factory() as session:
            await session.execute(delete(User).where(User.id == user.id))
            await session.commit()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_empty_final_response_is_rewritten_from_collected_evidence() -> None:
    gateway = ScriptedEmptyFinalResponseGateway()

    body, run, _ = await _turn(gateway, "Convert 2500 mcg to mg")

    assert run.status == "completed"
    assert run.orchestration_state.get("empty_response_recovered") is True
    assert "2.5 milligrams" in body
    assert "event: run.failed" not in body
    # The recovery attempt had tools disabled, so it could not start new research.
    assert gateway.calls[-1]["tools"] == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_repeated_calls_to_a_capped_tool_are_refused_with_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import chat_orchestrator

    monkeypatch.setattr(chat_orchestrator, "TOOL_REPEAT_LIMITS", {"convert_mass": 2})

    _, run, executions = await _turn(ScriptedRepeatingToolGateway(), "Convert four values")

    suppressed = [
        execution
        for execution in executions
        if (execution.result or {}).get("audit", {}).get("suppression_reason")
        == "tool_repeat_limit"
    ]
    assert run.status == "completed"
    assert len(executions) == 4
    assert len(suppressed) == 2
    assert suppressed[0].result is not None
    assert "already been used 2 times" in suppressed[0].result["data"]["message"]
