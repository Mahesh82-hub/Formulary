from collections.abc import Iterable
from uuid import UUID

from app.models import Message


class InvalidMessageBranchError(ValueError):
    """Raised when a conversation's active branch cannot be reconstructed."""


def build_active_message_path(
    messages: Iterable[Message],
    active_leaf_message_id: UUID | None,
) -> list[Message]:
    """Return the root-to-leaf path selected by a conversation's active leaf."""
    if active_leaf_message_id is None:
        return []

    messages_by_id = {message.id: message for message in messages}
    path: list[Message] = []
    visited: set[UUID] = set()
    current_id: UUID | None = active_leaf_message_id

    while current_id is not None:
        if current_id in visited:
            raise InvalidMessageBranchError("message branch contains a cycle")
        visited.add(current_id)

        message = messages_by_id.get(current_id)
        if message is None:
            raise InvalidMessageBranchError("message branch references a missing message")
        path.append(message)
        current_id = message.parent_message_id

    path.reverse()
    return path
