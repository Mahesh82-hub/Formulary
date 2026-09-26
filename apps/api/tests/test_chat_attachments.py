import io
from typing import Any
from uuid import uuid4

import pytest
from fastapi import UploadFile, status
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from starlette.datastructures import Headers

from app.api.dependencies.auth import get_current_user
from app.db.session import async_session_factory
from app.llm.gateway import get_llm_gateway
from app.llm.models import LLMCompletion
from app.main import app
from app.models import Conversation, Message, User
from app.services.chat_attachments import ChatAttachmentError, ChatPDFAttachmentProcessor
from app.services.chat_orchestrator import PDF_ATTACHMENT_SYSTEM_GUIDANCE, ChatOrchestrator


def _single_page_text_pdf(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode())
    payload.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
        ).encode()
    )
    return bytes(payload)


def _processor(
    *, max_bytes: int = 1_000_000, max_characters: int = 80_000
) -> ChatPDFAttachmentProcessor:
    return ChatPDFAttachmentProcessor(
        max_bytes=max_bytes,
        max_pages=10,
        max_extracted_characters=max_characters,
        native_text_min_characters=10,
        ocr_dpi=150,
    )


def _upload(payload: bytes, filename: str = "evidence.pdf") -> UploadFile:
    return UploadFile(
        io.BytesIO(payload),
        filename=filename,
        size=len(payload),
        headers=Headers({"content-type": "application/pdf"}),
    )


@pytest.mark.asyncio
async def test_chat_pdf_attachment_uses_existing_extraction_pipeline() -> None:
    payload = _single_page_text_pdf("Atorvastatin exposure evidence from the attached label")

    attachment = await _processor().process(_upload(payload, "../label-evidence"))

    assert attachment["filename"] == "label-evidence.pdf"
    assert attachment["media_type"] == "application/pdf"
    assert attachment["pages"] == 1
    assert attachment["native_text_pages"] == 1
    assert attachment["ocr_pages"] == 0
    assert attachment["byte_size"] == len(payload)
    assert "Atorvastatin exposure evidence" in attachment["extracted_text"]


@pytest.mark.asyncio
async def test_chat_pdf_attachment_rejects_non_pdf_bytes() -> None:
    with pytest.raises(ChatAttachmentError) as raised:
        await _processor().process(_upload(b"not a pdf"))

    assert raised.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert str(raised.value) == "The selected file is not a valid PDF"


@pytest.mark.asyncio
async def test_chat_pdf_attachment_enforces_upload_limit() -> None:
    with pytest.raises(ChatAttachmentError) as raised:
        await _processor(max_bytes=10).process(_upload(b"%PDF-" + b"x" * 20))

    assert raised.value.status_code == status.HTTP_413_CONTENT_TOO_LARGE


def test_orchestrator_includes_pdf_as_delimited_untrusted_context() -> None:
    message = Message(
        role="user",
        status="completed",
        plain_text="Summarize the endpoints",
        content=[
            {"type": "text", "text": "Summarize the endpoints"},
            {
                "type": "document",
                "media_type": "application/pdf",
                "filename": "study.pdf",
                "pages": 2,
                "extracted_text": "[Page 1]\nCmax was 120 ng/mL.",
            },
        ],
    )

    rendered = ChatOrchestrator._message_prompt_content(message)

    assert rendered.startswith("Summarize the endpoints")
    assert "[BEGIN USER-ATTACHED PDF: study.pdf, 2 pages]" in rendered
    assert "Cmax was 120 ng/mL." in rendered
    assert rendered.endswith("[END USER-ATTACHED PDF]")


class PDFContextGateway:
    def validate_selection(self, provider: str, model: str) -> None:
        assert provider == "groq"
        assert model == "test-model"

    async def complete(self, **kwargs: Any) -> LLMCompletion:
        assert PDF_ATTACHMENT_SYSTEM_GUIDANCE in kwargs["system_prompt"]
        assert "[BEGIN USER-ATTACHED PDF: study.pdf, 1 page]" in kwargs["messages"][-1].content
        assert "Atorvastatin label evidence" in kwargs["messages"][-1].content
        return LLMCompletion(
            provider_response_id="pdf-context-response",
            text="I reviewed the attached PDF.",
        )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pdf_chat_turn_extracts_persists_and_redacts_document_context() -> None:
    async with async_session_factory() as session:
        user = User(email=f"pdf-chat-{uuid4()}@example.com")
        session.add(user)
        await session.commit()
        await session.refresh(user)

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_llm_gateway] = PDFContextGateway
    conversation_id: str | None = None
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            conversation_response = await client.post("/api/v1/conversations", json={})
            assert conversation_response.status_code == 201
            conversation_id = conversation_response.json()["id"]

            turn_response = await client.post(
                f"/api/v1/conversations/{conversation_id}/turns/pdf",
                data={
                    "text": "Summarize the attached evidence",
                    "provider": "groq",
                    "model": "test-model",
                },
                files={
                    "pdf": (
                        "study.pdf",
                        _single_page_text_pdf("Atorvastatin label evidence"),
                        "application/pdf",
                    )
                },
            )

            assert turn_response.status_code == 200
            assert "event: message.completed" in turn_response.text
            assert "I reviewed the attached PDF." in turn_response.text

            detail_response = await client.get(f"/api/v1/conversations/{conversation_id}")
            user_message = detail_response.json()["messages"][0]
            document = user_message["content"][1]
            assert document["filename"] == "study.pdf"
            assert document["pages"] == 1
            assert "extracted_text" not in document

        async with async_session_factory() as session:
            conversation = await session.scalar(
                select(Conversation).where(Conversation.id == conversation_id)
            )
            assert conversation is not None
            message = await session.scalar(
                select(Message).where(
                    Message.conversation_id == conversation.id,
                    Message.role == "user",
                )
            )
            assert message is not None
            assert "Atorvastatin label evidence" in message.content[1]["extracted_text"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_llm_gateway, None)
        async with async_session_factory() as session:
            await session.execute(delete(User).where(User.id == user.id))
            await session.commit()
