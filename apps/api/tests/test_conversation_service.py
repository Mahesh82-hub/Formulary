from uuid import uuid4

import pytest

from app.models import Message
from app.services.conversations import InvalidMessageBranchError, build_active_message_path


def test_build_active_message_path_selects_only_active_branch() -> None:
    conversation_id = uuid4()
    root = Message(
        id=uuid4(),
        conversation_id=conversation_id,
        role="user",
        status="completed",
        content=[],
        plain_text="Question",
    )
    abandoned = Message(
        id=uuid4(),
        conversation_id=conversation_id,
        parent_message_id=root.id,
        role="assistant",
        status="completed",
        content=[],
        plain_text="First answer",
    )
    active = Message(
        id=uuid4(),
        conversation_id=conversation_id,
        parent_message_id=root.id,
        supersedes_message_id=abandoned.id,
        role="assistant",
        status="completed",
        content=[],
        plain_text="Regenerated answer",
    )

    path = build_active_message_path([root, abandoned, active], active.id)

    assert [message.id for message in path] == [root.id, active.id]


def test_build_active_message_path_rejects_missing_parent() -> None:
    message = Message(
        id=uuid4(),
        conversation_id=uuid4(),
        parent_message_id=uuid4(),
        role="user",
        status="completed",
        content=[],
        plain_text="Question",
    )

    with pytest.raises(InvalidMessageBranchError, match="missing"):
        build_active_message_path([message], message.id)
