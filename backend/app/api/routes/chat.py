import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.api.deps import CurrentUser, DbSession
from app.core.config import Settings, get_settings
from app.models.chat_generation import ChatGeneration
from app.models.conversation import Conversation, Message
from app.models.knowledge import KnowledgeDocument
from app.schemas.conversation import (
    ChatRequest,
    ChatResponse,
    SourceReference,
    StreamChatRequest,
)
from app.schemas.system import ModelInfoResponse
from app.services.embeddings import EmbeddingUnavailableError, embed_texts
from app.services.gemini_provider import ModelRateLimitError
from app.services.model_provider import (
    ModelUnavailableError,
    create_ai_provider,
)
from app.services.performance import log_stage
from app.services.conversations import get_owned_conversation, resolve_workspace_id
from app.services.generation_coordination import (
    GenerationCoordinationError,
    GenerationCoordinator,
    GenerationLease,
    get_generation_coordinator,
)
from app.services.retrieval import (
    EmbeddingCompatibilityError,
    associate_citations,
    build_rag_context,
    retrieve_chunks,
    validate_citations,
)

router = APIRouter(tags=["chat"])
logger = logging.getLogger(__name__)


@dataclass
class GenerationControl:
    cancel_requested: asyncio.Event
    lease: GenerationLease | None


async def _next_token_or_cancel(
    provider_stream: AsyncIterator[str], control: GenerationControl
) -> tuple[str | None, bool]:
    if control.cancel_requested.is_set():
        return None, True

    next_token = asyncio.create_task(anext(provider_stream))
    cancel_wait = asyncio.create_task(control.cancel_requested.wait())
    tasks = (next_token, cancel_wait)
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if control.cancel_requested.is_set():
            return None, True
        return next_token.result(), False
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@router.get("/model", response_model=ModelInfoResponse)
async def model_info(user: CurrentUser) -> ModelInfoResponse:
    settings = get_settings()
    return ModelInfoResponse(**create_ai_provider(settings).model_info())


@dataclass
class PreparedChat:
    settings: Settings
    message: str
    conversation: Conversation | None
    workspace_id: UUID
    history: list[dict[str, str]]
    rag_context: str | None
    sources: list[SourceReference]
    grounded: bool
    fixed_reply: str | None


async def _prepare_chat(
    body: ChatRequest,
    user: CurrentUser,
    db: DbSession,
    request_id: UUID | None = None,
) -> PreparedChat:
    history_started_at = time.perf_counter()
    settings = get_settings()
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="Message cannot be blank.")
    if len(message) > settings.max_message_chars:
        raise HTTPException(status_code=413, detail="Message is too long.")

    conversation: Conversation | None = None
    workspace_id = body.workspace_id
    history: list[dict[str, str]] = []
    if body.conversation_id is not None:
        conversation = get_owned_conversation(db, body.conversation_id, user)
        if (
            body.workspace_id is not None
            and body.workspace_id != conversation.workspace_id
        ):
            raise HTTPException(status_code=404, detail="Conversation not found.")
        workspace_id = conversation.workspace_id
        previous_messages = list(
            db.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.sequence.desc())
                .limit(10)
            )
        )
        history = [
            {"role": item.role, "content": item.content}
            for item in reversed(previous_messages)
            if item.status == "completed"
        ]
    else:
        workspace_id = resolve_workspace_id(db, user, body.workspace_id)

    if workspace_id is None:
        raise HTTPException(status_code=403, detail="Workspace membership required.")
    if request_id is not None:
        log_stage("conversation_history_loaded", history_started_at, request_id)
    return PreparedChat(
        settings=settings,
        message=message,
        conversation=conversation,
        workspace_id=workspace_id,
        history=history,
        rag_context=None,
        sources=[],
        grounded=False,
        fixed_reply=None,
    )


async def _prepare_rag(
    prepared: PreparedChat, db: DbSession, request_id: UUID | None = None
) -> None:
    retrieval_started_at = time.perf_counter()
    ready_documents = db.scalar(
        select(func.count())
        .select_from(KnowledgeDocument)
        .where(
            KnowledgeDocument.workspace_id == prepared.workspace_id,
            KnowledgeDocument.status == "ready",
        )
    ) or 0
    if request_id is not None:
        log_stage("rag_document_check_completed", retrieval_started_at, request_id)
    if ready_documents:
        try:
            task_type = (
                "RETRIEVAL_QUERY"
                if prepared.settings.embedding_provider == "gemini"
                else None
            )
            query_embedding = (
                await embed_texts(
                    prepared.settings,
                    [prepared.message],
                    request_id=request_id,
                    **({"task_type": task_type} if task_type is not None else {}),
                )
            )[0]
        except EmbeddingUnavailableError as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    "The configured embedding provider is unavailable. Check its "
                    "backend configuration and selected model."
                ),
            ) from exc
        retrieval_started_at = time.perf_counter()
        try:
            retrieved = retrieve_chunks(
                db,
                prepared.workspace_id,
                query_embedding,
                prepared.settings.knowledge_retrieval_count,
                prepared.settings.knowledge_min_similarity,
                prepared.settings.embedding_provider,
                prepared.settings.configured_embedding_model,
            )
        except EmbeddingCompatibilityError as exc:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Knowledge base embeddings are incompatible with the configured "
                    "embedding model. Re-upload and re-index documents before switching."
                ),
            ) from exc
        if request_id is not None:
            log_stage("rag_retrieval_completed", retrieval_started_at, request_id)
        if not retrieved:
            prepared.fixed_reply = (
                "The available documents do not contain enough information "
                "to answer that question."
            )
        else:
            context_started_at = time.perf_counter()
            prepared.rag_context, source_data = build_rag_context(retrieved)
            prepared.sources = [
                SourceReference.model_validate(source)
                for source in source_data
            ]
            prepared.grounded = True
            if request_id is not None:
                log_stage("rag_context_constructed", context_started_at, request_id)


def _persist_response(
    db: DbSession,
    user: CurrentUser,
    prepared: PreparedChat,
    reply: str,
    generation: ChatGeneration | None = None,
) -> ChatResponse:
    resolve_workspace_id(db, user, prepared.workspace_id)
    reply, source_data = validate_citations(
        db,
        prepared.workspace_id,
        reply,
        [source.model_dump(mode="json") for source in prepared.sources],
    )
    prepared.sources = [
        SourceReference.model_validate(source) for source in source_data
    ]
    prepared.grounded = prepared.grounded and bool(prepared.sources)
    if not reply.strip():
        raise HTTPException(
            status_code=502,
            detail="The response no longer has verifiable source references.",
        )
    conversation = prepared.conversation
    existing_messages: tuple[Message, Message] | None = None
    if (
        generation is not None
        and generation.user_message_id is not None
        and generation.assistant_message_id is not None
        and generation.conversation_id is not None
    ):
        conversation = get_owned_conversation(
            db, generation.conversation_id, user
        )
        if conversation.workspace_id != prepared.workspace_id:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        user_message = db.get(Message, generation.user_message_id)
        assistant_message = db.get(Message, generation.assistant_message_id)
        if (
            user_message is not None
            and assistant_message is not None
            and user_message.conversation_id == conversation.id
            and assistant_message.conversation_id == conversation.id
        ):
            existing_messages = (user_message, assistant_message)

    if conversation is None:
        conversation = Conversation(
            workspace_id=prepared.workspace_id,
            created_by_id=user.id,
            title=prepared.message[:200],
        )
        db.add(conversation)
        db.flush()

    conversation.updated_at = datetime.now(timezone.utc)
    if existing_messages is None:
        allocated_sequence = db.scalar(
            update(Conversation)
            .where(Conversation.id == conversation.id)
            .values(
                last_message_sequence=Conversation.last_message_sequence + 2
            )
            .returning(Conversation.last_message_sequence)
        )
        if allocated_sequence is None:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        next_sequence = allocated_sequence - 1
        user_message = Message(
            conversation_id=conversation.id,
            sequence=next_sequence,
            role="user",
            content=prepared.message,
            sources=[],
        )
        assistant_message = Message(
            conversation_id=conversation.id,
            sequence=next_sequence + 1,
            role="assistant",
            content=reply,
            sources=[
                source.model_dump(mode="json")
                for source in prepared.sources
            ],
        )
        db.add_all([user_message, assistant_message])
    else:
        user_message, assistant_message = existing_messages
        assistant_message.content = reply
        assistant_message.status = "completed"
        assistant_message.sources = [
            source.model_dump(mode="json")
            for source in prepared.sources
        ]
    db.flush()
    if generation is not None:
        generation.status = "completed"
        generation.conversation_id = conversation.id
        generation.user_message_id = user_message.id
        generation.assistant_message_id = assistant_message.id
        generation.reply = reply
        generation.model = prepared.settings.ollama_model
        generation.grounded = prepared.grounded
        generation.sources = [
            source.model_dump(mode="json") for source in prepared.sources
        ]
    db.commit()
    return ChatResponse(
        reply=reply,
        model=prepared.settings.ollama_model,
        conversation_id=conversation.id,
        grounded=prepared.grounded,
        sources=prepared.sources,
    )


def _persist_interrupted_response(
    db: DbSession,
    user: CurrentUser,
    prepared: PreparedChat,
    partial_reply: str,
    generation: ChatGeneration,
) -> tuple[Conversation, Message, Message]:
    resolve_workspace_id(db, user, prepared.workspace_id)
    conversation = prepared.conversation
    existing_messages: tuple[Message, Message] | None = None
    if (
        generation.conversation_id is not None
        and generation.user_message_id is not None
        and generation.assistant_message_id is not None
    ):
        conversation = get_owned_conversation(
            db, generation.conversation_id, user
        )
        if conversation.workspace_id != prepared.workspace_id:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        user_message = db.get(Message, generation.user_message_id)
        assistant_message = db.get(Message, generation.assistant_message_id)
        if (
            user_message is not None
            and assistant_message is not None
            and user_message.conversation_id == conversation.id
            and assistant_message.conversation_id == conversation.id
        ):
            existing_messages = (user_message, assistant_message)

    if conversation is None:
        conversation = Conversation(
            workspace_id=prepared.workspace_id,
            created_by_id=user.id,
            title=prepared.message[:200],
        )
        db.add(conversation)
        db.flush()

    conversation.updated_at = datetime.now(timezone.utc)
    if existing_messages is None:
        allocated_sequence = db.scalar(
            update(Conversation)
            .where(Conversation.id == conversation.id)
            .values(last_message_sequence=Conversation.last_message_sequence + 2)
            .returning(Conversation.last_message_sequence)
        )
        if allocated_sequence is None:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        next_sequence = allocated_sequence - 1
        user_message = Message(
            conversation_id=conversation.id,
            sequence=next_sequence,
            role="user",
            content=prepared.message,
            sources=[],
            status="completed",
        )
        assistant_message = Message(
            conversation_id=conversation.id,
            sequence=next_sequence + 1,
            role="assistant",
            content=partial_reply,
            sources=[],
            status="interrupted",
        )
        db.add_all([user_message, assistant_message])
    else:
        user_message, assistant_message = existing_messages
        assistant_message.content = partial_reply
        assistant_message.status = "interrupted"
        assistant_message.sources = []
    db.flush()
    generation.status = "cancelled"
    generation.conversation_id = conversation.id
    generation.user_message_id = user_message.id
    generation.assistant_message_id = assistant_message.id
    generation.reply = partial_reply
    generation.model = prepared.settings.ollama_model
    generation.grounded = False
    generation.sources = []
    db.commit()
    return conversation, user_message, assistant_message


def _request_fingerprint(body: StreamChatRequest) -> str:
    request_data = json.dumps(
        {
            "message": body.message.strip(),
            "conversation_id": str(body.conversation_id) if body.conversation_id else None,
            "workspace_id": str(body.workspace_id) if body.workspace_id else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(request_data.encode("utf-8")).hexdigest()


def _generation_is_stale(
    generation: ChatGeneration, stale_after_seconds: float
) -> bool:
    last_updated = generation.updated_at
    if last_updated.tzinfo is None:
        last_updated = last_updated.replace(tzinfo=timezone.utc)
    return (
        datetime.now(timezone.utc) - last_updated
    ).total_seconds() > stale_after_seconds


def _reserve_generation(
    db: DbSession,
    user: CurrentUser,
    body: StreamChatRequest,
    stale_after_seconds: float,
) -> tuple[ChatGeneration, bool]:
    fingerprint = _request_fingerprint(body)
    existing = db.scalar(
        select(ChatGeneration).where(
            ChatGeneration.user_id == user.id,
            ChatGeneration.request_id == body.request_id,
        )
    )
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="This request ID was already used for a different message.",
            )
        if existing.status == "completed":
            return existing, True
        if existing.status == "cancelled":
            existing.status = "processing"
            existing.reply = None
            db.commit()
            return existing, False
        if not _generation_is_stale(existing, stale_after_seconds):
            raise HTTPException(
                status_code=409,
                detail="This message is already being generated.",
            )
        db.delete(existing)
        db.commit()

    generation = ChatGeneration(
        user_id=user.id,
        request_id=body.request_id,
        request_fingerprint=fingerprint,
        status="processing",
    )
    db.add(generation)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        existing = db.scalar(
            select(ChatGeneration).where(
                ChatGeneration.user_id == user.id,
                ChatGeneration.request_id == body.request_id,
            )
        )
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="This request ID was already used for a different message.",
                ) from exc
            if existing.status == "completed":
                return existing, True
            if _generation_is_stale(existing, stale_after_seconds):
                db.delete(existing)
                db.commit()
                return _reserve_generation(
                    db, user, body, stale_after_seconds
                )
            raise HTTPException(
                status_code=409,
                detail="This message is already being generated.",
            ) from exc
        raise HTTPException(
            status_code=500, detail="The chat request could not be registered."
        ) from exc
    return generation, False


def _sse_event(event: str, payload: dict[str, object]) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n"


def _release_generation(db: DbSession, generation_id: UUID) -> None:
    try:
        db.rollback()
        generation = db.get(ChatGeneration, generation_id)
        if generation is not None and generation.status == "processing":
            db.delete(generation)
            db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Could not release interrupted chat request %s.", generation_id)


@router.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, user: CurrentUser, db: DbSession) -> ChatResponse:
    prepared = await _prepare_chat(body, user, db)
    try:
        await _prepare_rag(prepared, db)
        if prepared.fixed_reply is not None:
            reply = prepared.fixed_reply
        elif prepared.rag_context is None:
            reply = await create_ai_provider(prepared.settings).generate(
                prepared.message,
                prepared.history,
            )
        else:
            reply = await create_ai_provider(prepared.settings).generate(
                prepared.message,
                prepared.history,
                rag_context=prepared.rag_context,
            )
        reply, source_data = associate_citations(
            reply,
            [source.model_dump(mode="json") for source in prepared.sources],
        )
        prepared.sources = [
            SourceReference.model_validate(source) for source in source_data
        ]
        prepared.grounded = prepared.grounded and bool(prepared.sources)
        if not reply:
            raise HTTPException(status_code=502, detail="The model returned an empty response.")
        return _persist_response(db, user, prepared, reply)
    except ModelRateLimitError as exc:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="The configured AI provider is temporarily rate limited.",
            headers={"Retry-After": "2"},
        ) from exc
    except (ModelUnavailableError, EmbeddingUnavailableError) as exc:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="The configured AI provider is unavailable.",
        ) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=500, detail="The conversation could not be saved."
        ) from exc


@router.post("/chat/stream")
async def stream_chat(
    body: StreamChatRequest,
    user: CurrentUser,
    db: DbSession,
    coordinator: Annotated[
        GenerationCoordinator, Depends(get_generation_coordinator)
    ],
) -> StreamingResponse:
    prepared = await _prepare_chat(body, user, db, body.request_id)
    generation, replay = _reserve_generation(
        db,
        user,
        body,
        max(
            60.0,
            prepared.settings.ollama_read_timeout_seconds * 2,
            prepared.settings.ollama_timeout_seconds * 2,
        ),
    )
    lease = None
    if not replay:
        try:
            lease = await coordinator.register(
                generation.id, user.id, prepared.workspace_id
            )
        except GenerationCoordinationError as exc:
            _release_generation(db, generation.id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Generation coordination is unavailable.",
            ) from exc
    control = GenerationControl(
        cancel_requested=(
            lease.cancel_requested if lease is not None else asyncio.Event()
        ),
        lease=lease,
    )

    async def generate_events() -> AsyncIterator[str]:
        stream_started_at = time.perf_counter()
        completed = False
        cancelled = False
        provider_stream: AsyncIterator[str] | None = None
        parts: list[str] = []
        try:
            log_stage("sse_response_started", request_id=body.request_id)
            if replay:
                if generation.reply is None or generation.conversation_id is None:
                    yield _sse_event(
                        "error",
                        {"code": "invalid_replay", "message": "The saved response is unavailable."},
                    )
                    return
                replay_conversation = get_owned_conversation(
                    db, generation.conversation_id, user
                )
                if replay_conversation.workspace_id != prepared.workspace_id:
                    yield _sse_event(
                        "error",
                        {"code": "invalid_replay", "message": "The saved response is unavailable."},
                    )
                    return
                replay_reply, replay_sources = validate_citations(
                    db,
                    replay_conversation.workspace_id,
                    generation.reply,
                    generation.sources,
                )
                yield _sse_event(
                    "complete",
                    {
                        "reply": replay_reply,
                        "model": generation.model or prepared.settings.ollama_model,
                        "conversation_id": str(generation.conversation_id),
                        "grounded": generation.grounded and bool(replay_sources),
                        "sources": replay_sources,
                        "replayed": True,
                    },
                )
                completed = True
                return

            yield _sse_event("started", {"stage": "preparing"})
            if control.cancel_requested.is_set():
                was_cancelled = True
            else:
                rag_task = asyncio.create_task(
                    _prepare_rag(prepared, db, body.request_id)
                )
                cancel_wait = asyncio.create_task(control.cancel_requested.wait())
                try:
                    done, _ = await asyncio.wait(
                        (rag_task, cancel_wait),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if control.cancel_requested.is_set():
                        rag_task.cancel()
                        await asyncio.gather(rag_task, return_exceptions=True)
                        was_cancelled = True
                    else:
                        await rag_task
                        was_cancelled = False
                except (asyncio.CancelledError, GeneratorExit):
                    rag_task.cancel()
                    await asyncio.gather(rag_task, return_exceptions=True)
                    raise
                finally:
                    if not cancel_wait.done():
                        cancel_wait.cancel()
                    await asyncio.gather(cancel_wait, return_exceptions=True)

            if was_cancelled:
                pass
            elif prepared.fixed_reply is not None:
                parts.append(prepared.fixed_reply)
                log_stage("first_token_sent", stream_started_at, body.request_id)
                yield _sse_event("token", {"token": prepared.fixed_reply})
                was_cancelled = control.cancel_requested.is_set()
            else:
                provider = create_ai_provider(prepared.settings)
                provider.performance_request_id = body.request_id
                provider_stream = provider.stream(
                    prepared.message,
                    prepared.history,
                    rag_context=prepared.rag_context,
                )
                last_heartbeat = time.monotonic()
                was_cancelled = False
                while True:
                    try:
                        token, was_cancelled = await _next_token_or_cancel(
                            provider_stream, control
                        )
                    except StopAsyncIteration:
                        break
                    if was_cancelled:
                        break
                    if token is None:
                        continue
                    if not parts:
                        log_stage("first_token_sent", stream_started_at, body.request_id)
                    if time.monotonic() - last_heartbeat >= 15:
                        db.execute(
                            update(ChatGeneration)
                            .where(
                                ChatGeneration.id == generation.id,
                                ChatGeneration.status == "processing",
                            )
                            .values(updated_at=datetime.now(timezone.utc))
                        )
                        db.commit()
                        last_heartbeat = time.monotonic()
                    parts.append(token)
                    yield _sse_event("token", {"token": token})

            if provider_stream is not None:
                await provider_stream.aclose()
                provider_stream = None

            if was_cancelled:
                log_stage("generation_cancelled", stream_started_at, body.request_id)
                partial_reply = "".join(parts).strip()
                try:
                    conversation, user_message, assistant_message = (
                        _persist_interrupted_response(
                            db, user, prepared, partial_reply, generation
                        )
                    )
                except SQLAlchemyError:
                    db.rollback()
                    logger.exception(
                        "Could not persist cancelled chat request %s.",
                        body.request_id,
                    )
                    yield _sse_event(
                        "error",
                        {
                            "code": "cancellation_persistence_error",
                            "message": "The interrupted response could not be saved.",
                        },
                    )
                    return
                cancelled = True
                yield _sse_event(
                    "cancelled",
                    {
                        "reply": partial_reply,
                        "model": prepared.settings.ollama_model,
                        "conversation_id": str(conversation.id),
                        "user_message_id": str(user_message.id),
                        "assistant_message_id": str(assistant_message.id),
                        "status": "interrupted",
                        "grounded": False,
                        "sources": [],
                    },
                )
                return

            reply, source_data = associate_citations(
                "".join(parts).strip(),
                [source.model_dump(mode="json") for source in prepared.sources],
            )
            prepared.sources = [
                SourceReference.model_validate(source) for source in source_data
            ]
            prepared.grounded = prepared.grounded and bool(prepared.sources)
            if not reply:
                yield _sse_event(
                    "error",
                    {"code": "empty_response", "message": "The model returned an empty response."},
                )
                return
            try:
                assert control.lease is not None
                may_complete = await coordinator.begin_completion(control.lease)
            except GenerationCoordinationError:
                may_complete = False
            if not may_complete:
                was_cancelled = True
            if was_cancelled:
                partial_reply = "".join(parts).strip()
                try:
                    conversation, user_message, assistant_message = (
                        _persist_interrupted_response(
                            db, user, prepared, partial_reply, generation
                        )
                    )
                except SQLAlchemyError:
                    db.rollback()
                    logger.exception(
                        "Could not persist cancelled chat request %s.",
                        body.request_id,
                    )
                    yield _sse_event(
                        "error",
                        {
                            "code": "cancellation_persistence_error",
                            "message": "The interrupted response could not be saved.",
                        },
                    )
                    return
                cancelled = True
                yield _sse_event(
                    "cancelled",
                    {
                        "reply": partial_reply,
                        "model": prepared.settings.ollama_model,
                        "conversation_id": str(conversation.id),
                        "user_message_id": str(user_message.id),
                        "assistant_message_id": str(assistant_message.id),
                        "status": "interrupted",
                        "grounded": False,
                        "sources": [],
                    },
                )
                return
            try:
                result = _persist_response(db, user, prepared, reply, generation)
            except SQLAlchemyError:
                db.rollback()
                logger.exception("Could not save streamed chat request %s.", body.request_id)
                yield _sse_event(
                    "error",
                    {"code": "persistence_error", "message": "The response could not be saved."},
                )
                return
            completed = True
            log_stage("generation_completed", stream_started_at, body.request_id)
            yield _sse_event(
                "complete",
                {
                    **result.model_dump(mode="json"),
                    "replayed": False,
                },
            )
        except (asyncio.CancelledError, GeneratorExit):
            control.cancel_requested.set()
            log_stage("generation_disconnected", stream_started_at, body.request_id)
            if provider_stream is not None:
                try:
                    await provider_stream.aclose()
                except Exception:
                    logger.exception(
                        "Could not close provider stream for request %s.",
                        body.request_id,
                    )
                provider_stream = None
            if not completed and not cancelled:
                try:
                    _persist_interrupted_response(
                        db, user, prepared, "".join(parts).strip(), generation
                    )
                    cancelled = True
                except Exception:
                    db.rollback()
                    logger.exception(
                        "Could not persist disconnected chat request %s.",
                        body.request_id,
                    )
            raise
        except ModelRateLimitError:
            db.rollback()
            log_stage("generation_failed", stream_started_at, body.request_id)
            yield _sse_event(
                "error",
                {
                    "code": "provider_rate_limited",
                    "message": "The configured AI provider is temporarily rate limited.",
                },
            )
        except ModelUnavailableError:
            db.rollback()
            log_stage("generation_failed", stream_started_at, body.request_id)
            yield _sse_event(
                "error",
                {
                    "code": "provider_error",
                    "message": "The configured AI provider interrupted the response. No messages were saved.",
                },
            )
        except Exception:
            logger.exception("Unexpected error while streaming chat request %s.", body.request_id)
            log_stage("generation_failed", stream_started_at, body.request_id)
            yield _sse_event(
                "error",
                {
                    "code": "stream_error",
                    "message": "The response could not be completed. No messages were saved.",
                },
            )
        finally:
            if provider_stream is not None:
                try:
                    await provider_stream.aclose()
                except Exception:
                    logger.exception(
                        "Could not close provider stream for request %s.",
                        body.request_id,
                    )
            if not completed and not cancelled:
                _release_generation(db, generation.id)
            if control.lease is not None:
                await coordinator.finish(control.lease)

    return StreamingResponse(
        generate_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/chat/stream/{request_id}/cancel", status_code=202)
async def cancel_stream(
    request_id: UUID,
    user: CurrentUser,
    db: DbSession,
    coordinator: Annotated[
        GenerationCoordinator, Depends(get_generation_coordinator)
    ],
) -> dict[str, str]:
    generation = db.scalar(
        select(ChatGeneration).where(
            ChatGeneration.user_id == user.id,
            ChatGeneration.request_id == request_id,
        )
    )
    if generation is None:
        raise HTTPException(status_code=404, detail="Generation not found.")
    if generation.status == "completed":
        if generation.conversation_id is not None:
            get_owned_conversation(db, generation.conversation_id, user)
        raise HTTPException(status_code=409, detail="Generation has already completed.")
    if generation.status == "cancelled":
        if generation.conversation_id is not None:
            get_owned_conversation(db, generation.conversation_id, user)
        return {"status": "cancellation_requested"}

    try:
        workspace_id = await coordinator.workspace_for(generation.id, user.id)
    except GenerationCoordinationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Generation coordination is unavailable.",
        ) from exc
    if workspace_id is None:
        raise HTTPException(
            status_code=503,
            detail="The active generation is no longer available.",
        )
    resolve_workspace_id(db, user, workspace_id)
    try:
        accepted = await coordinator.request_cancel(
            generation.id, user.id, workspace_id
        )
    except GenerationCoordinationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Generation coordination is unavailable.",
        ) from exc
    if not accepted:
        db.refresh(generation)
        if generation.status == "completed":
            raise HTTPException(status_code=409, detail="Generation has already completed.")
        if generation.status == "cancelled":
            return {"status": "cancellation_requested"}
        raise HTTPException(status_code=409, detail="Generation has already completed.")
    return {"status": "cancellation_requested"}
