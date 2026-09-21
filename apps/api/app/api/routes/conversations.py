from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import get_current_user
from app.core.config import get_settings
from app.db.session import get_db_session
from app.llm.gateway import LLMGateway, ProviderName, get_llm_gateway
from app.llm.provider import LLMConfigurationError
from app.mcp_gateway.client import FastMCPToolClient, get_mcp_tool_client
from app.models import AssistantRun, Conversation, Message, User
from app.schemas.chat import (
    ChatTurnRequest,
    ConversationCreate,
    ConversationDetail,
    ConversationResponse,
    ConversationUpdate,
    MessageCreate,
    MessageEdit,
    MessageResponse,
    RegenerateRequest,
)
from app.services.chat_attachments import ChatAttachmentError, ChatPDFAttachmentProcessor
from app.services.chat_orchestrator import ChatOrchestrator
from app.services.conversations import InvalidMessageBranchError, build_active_message_path

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])
DatabaseSession = Annotated[AsyncSession, Depends(get_db_session)]
CurrentUser = Annotated[User, Depends(get_current_user)]
LLMGatewayDependency = Annotated[LLMGateway, Depends(get_llm_gateway)]
MCPToolClientDependency = Annotated[FastMCPToolClient, Depends(get_mcp_tool_client)]


async def owned_conversation(
    session: AsyncSession,
    conversation_id: UUID,
    user_id: UUID,
    *,
    for_update: bool = False,
) -> Conversation:
    statement = select(Conversation).where(
        Conversation.id == conversation_id,
        Conversation.user_id == user_id,
        Conversation.deleted_at.is_(None),
    )
    if for_update:
        statement = statement.with_for_update()
    conversation = await session.scalar(statement)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return conversation


def conversation_response(conversation: Conversation) -> ConversationResponse:
    return ConversationResponse.model_validate(conversation)


def message_response(message: Message) -> MessageResponse:
    response = MessageResponse.model_validate(message)
    public_content: list[dict[str, object]] = []
    for block in response.content:
        public_block = dict(block)
        if public_block.get("type") == "document":
            public_block.pop("extracted_text", None)
        public_content.append(public_block)
    return response.model_copy(update={"content": public_content})


@router.post("", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    payload: ConversationCreate,
    user: CurrentUser,
    session: DatabaseSession,
) -> ConversationResponse:
    conversation = Conversation(
        user_id=user.id,
        title=payload.title,
        model_preferences=payload.model_preferences,
    )
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return conversation_response(conversation)


@router.get("", response_model=list[ConversationResponse])
async def list_conversations(
    user: CurrentUser,
    session: DatabaseSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ConversationResponse]:
    conversations = (
        await session.scalars(
            select(Conversation)
            .where(
                Conversation.user_id == user.id,
                Conversation.deleted_at.is_(None),
            )
            .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [conversation_response(conversation) for conversation in conversations]


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: UUID,
    user: CurrentUser,
    session: DatabaseSession,
) -> ConversationDetail:
    conversation = await owned_conversation(session, conversation_id, user.id)
    messages = (
        await session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at, Message.id)
        )
    ).all()
    try:
        active_messages = build_active_message_path(messages, conversation.active_leaf_message_id)
    except InvalidMessageBranchError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Conversation branch is inconsistent",
        ) from error

    response = conversation_response(conversation)
    return ConversationDetail(
        **response.model_dump(),
        messages=[message_response(message) for message in active_messages],
    )


@router.patch("/{conversation_id}", response_model=ConversationResponse)
async def update_conversation(
    conversation_id: UUID,
    payload: ConversationUpdate,
    user: CurrentUser,
    session: DatabaseSession,
) -> ConversationResponse:
    conversation = await owned_conversation(session, conversation_id, user.id, for_update=True)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(conversation, field, value)
    await session.commit()
    await session.refresh(conversation)
    return conversation_response(conversation)


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: UUID,
    user: CurrentUser,
    session: DatabaseSession,
) -> None:
    conversation = await owned_conversation(session, conversation_id, user.id, for_update=True)
    conversation.deleted_at = datetime.now(UTC)
    await session.commit()


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_user_message(
    conversation_id: UUID,
    payload: MessageCreate,
    user: CurrentUser,
    session: DatabaseSession,
) -> MessageResponse:
    conversation = await owned_conversation(session, conversation_id, user.id, for_update=True)
    parent_message_id = payload.parent_message_id or conversation.active_leaf_message_id
    if parent_message_id is not None:
        parent_exists = await session.scalar(
            select(Message.id).where(
                Message.id == parent_message_id,
                Message.conversation_id == conversation.id,
            )
        )
        if parent_exists is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Parent message does not belong to this conversation",
            )

    message = Message(
        conversation_id=conversation.id,
        parent_message_id=parent_message_id,
        created_by_user_id=user.id,
        role="user",
        status="completed",
        content=[{"type": "text", "text": payload.text}],
        plain_text=payload.text,
        completed_at=datetime.now(UTC),
    )
    session.add(message)
    await session.flush()
    conversation.active_leaf_message_id = message.id
    await session.commit()
    await session.refresh(message)
    return message_response(message)


@router.post(
    "/{conversation_id}/messages/{message_id}/edit",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def edit_user_message(
    conversation_id: UUID,
    message_id: UUID,
    payload: MessageEdit,
    user: CurrentUser,
    session: DatabaseSession,
) -> MessageResponse:
    conversation = await owned_conversation(session, conversation_id, user.id, for_update=True)
    original = await session.scalar(
        select(Message).where(
            Message.id == message_id,
            Message.conversation_id == conversation.id,
            Message.role == "user",
        )
    )
    if original is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User message not found")

    document_blocks = [
        dict(block) for block in original.content if block.get("type") == "document"
    ]
    replacement = Message(
        conversation_id=conversation.id,
        parent_message_id=original.parent_message_id,
        supersedes_message_id=original.id,
        created_by_user_id=user.id,
        role="user",
        status="completed",
        content=[{"type": "text", "text": payload.text}, *document_blocks],
        plain_text=payload.text,
        completed_at=datetime.now(UTC),
    )
    session.add(replacement)
    await session.flush()
    conversation.active_leaf_message_id = replacement.id
    await session.commit()
    await session.refresh(replacement)
    return message_response(replacement)


@router.post("/{conversation_id}/turns", response_class=StreamingResponse)
async def create_chat_turn(
    conversation_id: UUID,
    payload: ChatTurnRequest,
    user: CurrentUser,
    session: DatabaseSession,
    gateway: LLMGatewayDependency,
    mcp_tools: MCPToolClientDependency,
) -> StreamingResponse:
    return await _start_chat_turn(
        conversation_id=conversation_id,
        payload=payload,
        user=user,
        session=session,
        gateway=gateway,
        mcp_tools=mcp_tools,
    )


@router.post("/{conversation_id}/turns/pdf", response_class=StreamingResponse)
async def create_chat_turn_with_pdf(
    conversation_id: UUID,
    text: Annotated[str, Form(min_length=1, max_length=100_000)],
    pdf: Annotated[UploadFile, File()],
    user: CurrentUser,
    session: DatabaseSession,
    gateway: LLMGatewayDependency,
    mcp_tools: MCPToolClientDependency,
    provider: Annotated[ProviderName | None, Form()] = None,
    model: Annotated[str | None, Form(max_length=128)] = None,
    parent_message_id: Annotated[UUID | None, Form()] = None,
) -> StreamingResponse:
    # Reject unknown conversations before performing CPU-intensive document extraction.
    await owned_conversation(session, conversation_id, user.id)
    await session.rollback()
    try:
        payload = ChatTurnRequest(
            text=text,
            provider=provider,
            model=model,
            parent_message_id=parent_message_id,
        )
    except ValidationError as error:
        details = "; ".join(str(item["msg"]) for item in error.errors())
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=details or "Invalid chat request",
        ) from error

    settings = get_settings()
    processor = ChatPDFAttachmentProcessor(
        max_bytes=settings.chat_pdf_max_bytes,
        max_pages=settings.chat_pdf_max_pages,
        max_extracted_characters=settings.chat_pdf_max_extracted_characters,
        native_text_min_characters=settings.pdf_native_text_min_characters,
        ocr_dpi=settings.pdf_ocr_dpi,
    )
    try:
        attachment = await processor.process(pdf)
    except ChatAttachmentError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error

    return await _start_chat_turn(
        conversation_id=conversation_id,
        payload=payload,
        user=user,
        session=session,
        gateway=gateway,
        mcp_tools=mcp_tools,
        attachment=attachment,
    )


async def _start_chat_turn(
    *,
    conversation_id: UUID,
    payload: ChatTurnRequest,
    user: User,
    session: AsyncSession,
    gateway: LLMGateway,
    mcp_tools: FastMCPToolClient,
    attachment: dict[str, object] | None = None,
) -> StreamingResponse:
    conversation = await owned_conversation(session, conversation_id, user.id, for_update=True)
    if conversation.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Archived conversations cannot receive new messages",
        )

    active_run = await session.scalar(
        select(AssistantRun.id).where(
            AssistantRun.conversation_id == conversation.id,
            AssistantRun.status.in_(("queued", "running", "awaiting_approval")),
        )
    )
    if active_run is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An assistant response is already in progress",
        )

    provider = _resolve_provider(payload, conversation)
    model = _resolve_model(payload, conversation)
    try:
        gateway.validate_selection(provider, model)
    except LLMConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The selected model provider is not configured",
        ) from error

    parent_message_id = payload.parent_message_id or conversation.active_leaf_message_id
    if parent_message_id is not None:
        parent_exists = await session.scalar(
            select(Message.id).where(
                Message.id == parent_message_id,
                Message.conversation_id == conversation.id,
            )
        )
        if parent_exists is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Parent message does not belong to this conversation",
            )

    user_message = Message(
        conversation_id=conversation.id,
        parent_message_id=parent_message_id,
        created_by_user_id=user.id,
        role="user",
        status="completed",
        content=[
            {"type": "text", "text": payload.text},
            *([attachment] if attachment is not None else []),
        ],
        plain_text=payload.text,
        completed_at=datetime.now(UTC),
    )
    session.add(user_message)
    await session.flush()
    conversation.active_leaf_message_id = user_message.id
    run = AssistantRun(
        conversation_id=conversation.id,
        trigger_message_id=user_message.id,
        provider=provider,
        model=model,
        status="queued",
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)

    orchestrator = ChatOrchestrator(gateway, mcp_tools, get_settings())

    async def event_stream() -> AsyncIterator[str]:
        async for event in orchestrator.stream_run(run.id):
            yield event.encode()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/{conversation_id}/messages/{message_id}/regenerate",
    response_class=StreamingResponse,
)
async def regenerate_message(
    conversation_id: UUID,
    message_id: UUID,
    payload: RegenerateRequest,
    user: CurrentUser,
    session: DatabaseSession,
    gateway: LLMGatewayDependency,
    mcp_tools: MCPToolClientDependency,
) -> StreamingResponse:
    conversation = await owned_conversation(session, conversation_id, user.id, for_update=True)
    if conversation.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Archived conversations cannot regenerate responses",
        )
    active_run = await session.scalar(
        select(AssistantRun.id).where(
            AssistantRun.conversation_id == conversation.id,
            AssistantRun.status.in_(("queued", "running", "awaiting_approval")),
        )
    )
    if active_run is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An assistant response is already in progress",
        )

    target = await session.scalar(
        select(Message).where(
            Message.id == message_id,
            Message.conversation_id == conversation.id,
            Message.role.in_(("user", "assistant")),
        )
    )
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")

    supersedes_message_id: UUID | None = None
    retry_of_run_id: UUID | None = None
    if target.role == "user":
        trigger = target
    else:
        if target.parent_message_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Assistant message cannot be regenerated",
            )
        parent_trigger = await session.scalar(
            select(Message).where(
                Message.id == target.parent_message_id,
                Message.conversation_id == conversation.id,
                Message.role == "user",
            )
        )
        if parent_trigger is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Assistant message has no user prompt",
            )
        trigger = parent_trigger
        supersedes_message_id = target.id
        retry_of_run_id = await session.scalar(
            select(AssistantRun.id).where(AssistantRun.response_message_id == target.id)
        )

    provider = _resolve_provider_values(payload.provider, conversation)
    model = _resolve_model_values(payload.model, conversation)
    try:
        gateway.validate_selection(provider, model)
    except LLMConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The selected model provider is not configured",
        ) from error

    conversation.active_leaf_message_id = trigger.id
    run = AssistantRun(
        conversation_id=conversation.id,
        trigger_message_id=trigger.id,
        retry_of_run_id=retry_of_run_id,
        provider=provider,
        model=model,
        status="queued",
        orchestration_state=(
            {"supersedes_message_id": str(supersedes_message_id)}
            if supersedes_message_id is not None
            else {}
        ),
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)

    orchestrator = ChatOrchestrator(gateway, mcp_tools, get_settings())

    async def event_stream() -> AsyncIterator[str]:
        async for event in orchestrator.stream_run(run.id):
            yield event.encode()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _resolve_provider(payload: ChatTurnRequest, conversation: Conversation) -> ProviderName:
    return _resolve_provider_values(payload.provider, conversation)


def _resolve_provider_values(
    provider: ProviderName | None, conversation: Conversation
) -> ProviderName:
    if provider is not None:
        return provider
    preference = conversation.model_preferences.get("provider")
    if preference in {"openai", "groq"}:
        return "openai" if preference == "openai" else "groq"
    return get_settings().default_llm_provider


def _resolve_model(payload: ChatTurnRequest, conversation: Conversation) -> str:
    return _resolve_model_values(payload.model, conversation)


def _resolve_model_values(model: str | None, conversation: Conversation) -> str:
    if model is not None:
        return model
    preference = conversation.model_preferences.get("model")
    if isinstance(preference, str) and preference.strip():
        return preference.strip()
    return get_settings().default_llm_model.strip()
