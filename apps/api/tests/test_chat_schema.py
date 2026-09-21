from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select, update

from app.db.session import engine
from app.models import Conversation, Message, User


@pytest.mark.asyncio
@pytest.mark.integration
async def test_conversation_supports_regenerated_message_branches() -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            user_id = (
                await connection.execute(
                    insert(User).values(email=f"schema-{uuid4()}@example.com").returning(User.id)
                )
            ).scalar_one()
            conversation_id = (
                await connection.execute(
                    insert(Conversation)
                    .values(user_id=user_id, title="Branching schema test")
                    .returning(Conversation.id)
                )
            ).scalar_one()
            user_message_id = (
                await connection.execute(
                    insert(Message)
                    .values(
                        conversation_id=conversation_id,
                        created_by_user_id=user_id,
                        role="user",
                        status="completed",
                        content=[{"type": "text", "text": "Explain this medicine."}],
                        plain_text="Explain this medicine.",
                    )
                    .returning(Message.id)
                )
            ).scalar_one()

            first_answer_id = (
                await connection.execute(
                    insert(Message)
                    .values(
                        conversation_id=conversation_id,
                        parent_message_id=user_message_id,
                        role="assistant",
                        status="completed",
                        content=[{"type": "text", "text": "First answer"}],
                        plain_text="First answer",
                    )
                    .returning(Message.id)
                )
            ).scalar_one()
            regenerated_answer_id = (
                await connection.execute(
                    insert(Message)
                    .values(
                        conversation_id=conversation_id,
                        parent_message_id=user_message_id,
                        supersedes_message_id=first_answer_id,
                        role="assistant",
                        status="completed",
                        content=[{"type": "text", "text": "Regenerated answer"}],
                        plain_text="Regenerated answer",
                    )
                    .returning(Message.id)
                )
            ).scalar_one()
            await connection.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(active_leaf_message_id=regenerated_answer_id)
            )

            sibling_count = (
                await connection.execute(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.parent_message_id == user_message_id)
                )
            ).scalar_one()
            active_leaf = (
                await connection.execute(
                    select(Conversation.active_leaf_message_id).where(
                        Conversation.id == conversation_id
                    )
                )
            ).scalar_one()

            assert sibling_count == 2
            assert active_leaf == regenerated_answer_id
        finally:
            await transaction.rollback()
