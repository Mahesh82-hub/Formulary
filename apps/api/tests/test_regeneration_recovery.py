from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.core.config import get_settings
from app.db.session import async_session_factory
from app.models import AssistantRun, Conversation, Message, User
from app.services.chat_orchestrator import ChatOrchestrator


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cancelled_regeneration_restores_previous_assistant_branch() -> None:
    async with async_session_factory() as session:
        user = User(email=f"cancel-regeneration-{uuid4()}@example.com")
        session.add(user)
        await session.flush()

        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()

        prompt = Message(
            conversation_id=conversation.id,
            created_by_user_id=user.id,
            role="user",
            status="completed",
            content=[{"type": "text", "text": "Explain atorvastatin"}],
            plain_text="Explain atorvastatin",
            completed_at=datetime.now(UTC),
        )
        session.add(prompt)
        await session.flush()

        previous_response = Message(
            conversation_id=conversation.id,
            parent_message_id=prompt.id,
            role="assistant",
            status="completed",
            content=[{"type": "text", "text": "Previous answer"}],
            plain_text="Previous answer",
            completed_at=datetime.now(UTC),
        )
        session.add(previous_response)
        await session.flush()

        # Regeneration temporarily points the active branch at its user prompt.
        conversation.active_leaf_message_id = prompt.id
        run = AssistantRun(
            conversation_id=conversation.id,
            trigger_message_id=prompt.id,
            provider="groq",
            model="test-model",
            status="running",
            orchestration_state={"supersedes_message_id": str(previous_response.id)},
        )
        session.add(run)
        await session.commit()
        run_id = run.id
        user_id = user.id
        conversation_id = conversation.id
        previous_response_id = previous_response.id

    orchestrator = ChatOrchestrator(
        cast(Any, None),
        cast(Any, None),
        get_settings(),
    )
    await orchestrator._mark_cancelled(run_id)

    try:
        async with async_session_factory() as session:
            restored_run = await session.get(AssistantRun, run_id)
            restored_conversation = await session.get(Conversation, conversation_id)
            assert restored_run is not None
            assert restored_run.status == "cancelled"
            assert restored_run.orchestration_state["restored_superseded_message_id"] == str(
                previous_response_id
            )
            assert restored_conversation is not None
            assert restored_conversation.active_leaf_message_id == previous_response_id
    finally:
        async with async_session_factory() as session:
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()
