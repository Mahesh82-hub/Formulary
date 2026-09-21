from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.api.dependencies.auth import get_current_user
from app.db.session import async_session_factory
from app.main import app
from app.models import User


@pytest.mark.asyncio
@pytest.mark.integration
async def test_authenticated_conversation_and_message_branch_flow() -> None:
    async with async_session_factory() as session:
        user = User(email=f"chat-{uuid4()}@example.com")
        session.add(user)
        await session.commit()
        await session.refresh(user)

    app.dependency_overrides[get_current_user] = lambda: user
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            create_response = await client.post(
                "/api/v1/conversations",
                json={
                    "title": "  Medicine research  ",
                    "model_preferences": {"provider": "groq"},
                },
            )
            assert create_response.status_code == 201
            conversation = create_response.json()
            conversation_id = conversation["id"]
            assert conversation["title"] == "Medicine research"
            assert conversation["active_leaf_message_id"] is None

            first_response = await client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                json={"text": "  What is metformin?  "},
            )
            assert first_response.status_code == 201
            first_message = first_response.json()
            assert first_message["plain_text"] == "What is metformin?"
            assert first_message["parent_message_id"] is None

            second_response = await client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                json={"text": "Focus on approved indications."},
            )
            assert second_response.status_code == 201
            assert second_response.json()["parent_message_id"] == first_message["id"]

            edit_response = await client.post(
                f"/api/v1/conversations/{conversation_id}/messages/{first_message['id']}/edit",
                json={"text": "What are metformin's FDA-approved indications?"},
            )
            assert edit_response.status_code == 201
            edited_message = edit_response.json()
            assert edited_message["parent_message_id"] is None
            assert edited_message["supersedes_message_id"] == first_message["id"]

            follow_up_response = await client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                json={"text": "Include the label source later."},
            )
            assert follow_up_response.status_code == 201

            detail_response = await client.get(f"/api/v1/conversations/{conversation_id}")
            assert detail_response.status_code == 200
            detail = detail_response.json()
            assert [message["plain_text"] for message in detail["messages"]] == [
                "What are metformin's FDA-approved indications?",
                "Include the label source later.",
            ]
            assert detail["active_leaf_message_id"] == follow_up_response.json()["id"]

            list_response = await client.get("/api/v1/conversations")
            assert list_response.status_code == 200
            assert [item["id"] for item in list_response.json()] == [conversation_id]

            patch_response = await client.patch(
                f"/api/v1/conversations/{conversation_id}",
                json={"title": "Metformin", "status": "archived"},
            )
            assert patch_response.status_code == 200
            assert patch_response.json()["title"] == "Metformin"
            assert patch_response.json()["status"] == "archived"

            delete_response = await client.delete(f"/api/v1/conversations/{conversation_id}")
            assert delete_response.status_code == 204
            assert (await client.get(f"/api/v1/conversations/{conversation_id}")).status_code == 404
            assert (await client.get("/api/v1/conversations")).json() == []
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        async with async_session_factory() as session:
            await session.execute(delete(User).where(User.id == user.id))
            await session.commit()
