from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query, Response, status

from app.api.deps import CurrentUser, DbSession
from app.models.conversation import Conversation, Message
from app.schemas.conversation import (
    ConversationCreate,
    ConversationDocumentsUpdate,
    ConversationRename,
    ConversationResponse,
    MessageResponse,
    SourceReference,
)
from app.services.conversations import (
    create_conversation,
    delete_conversation,
    get_owned_conversation,
    list_messages,
    list_owned_conversations,
    rename_conversation,
    update_conversation_documents,
)
from app.services.retrieval import validate_citations

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _conversation_response(conversation: Conversation) -> ConversationResponse:
    return ConversationResponse(
        id=conversation.id,
        workspace_id=conversation.workspace_id,
        title=conversation.title,
        chat_type=conversation.chat_type,
        document_ids=[
            association.document_id
            for association in conversation.document_associations
        ],
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def _message_response(
    db: DbSession,
    workspace_id: UUID,
    message: Message,
    allowed_document_ids: set[UUID] | None = None,
) -> MessageResponse:
    content, associated_sources = validate_citations(
        db,
        workspace_id,
        message.content,
        message.sources or [],
        allowed_document_ids=allowed_document_ids,
    )
    return MessageResponse(
        id=message.id,
        role=message.role,
        content=content,
        created_at=message.created_at,
        sources=[
            SourceReference.model_validate(source) for source in associated_sources
        ],
        status=message.status,
    )


@router.post(
    "",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
def post_conversation(
    body: ConversationCreate,
    user: CurrentUser,
    db: DbSession,
) -> ConversationResponse:
    return _conversation_response(
        create_conversation(
            db,
            user,
            body.workspace_id,
            body.title,
            body.chat_type,
            body.document_ids,
        )
    )


@router.get("", response_model=list[ConversationResponse])
def get_conversations(
    user: CurrentUser,
    db: DbSession,
    workspace_id: UUID | None = Query(default=None),
    chat_type: Literal["normal", "knowledge"] | None = Query(default=None),
) -> list[ConversationResponse]:
    return [
        _conversation_response(conversation)
        for conversation in list_owned_conversations(
            db, user, workspace_id, chat_type
        )
    ]


@router.get("/{conversation_id}", response_model=ConversationResponse)
def get_conversation(
    conversation_id: UUID,
    user: CurrentUser,
    db: DbSession,
) -> ConversationResponse:
    return _conversation_response(get_owned_conversation(db, conversation_id, user))


@router.patch("/{conversation_id}", response_model=ConversationResponse)
def patch_conversation(
    conversation_id: UUID,
    body: ConversationRename,
    user: CurrentUser,
    db: DbSession,
) -> ConversationResponse:
    return _conversation_response(
        rename_conversation(db, conversation_id, user, body.title)
    )


@router.put(
    "/{conversation_id}/documents",
    response_model=ConversationResponse,
)
def put_conversation_documents(
    conversation_id: UUID,
    body: ConversationDocumentsUpdate,
    user: CurrentUser,
    db: DbSession,
) -> ConversationResponse:
    return _conversation_response(
        update_conversation_documents(
            db, conversation_id, user, body.document_ids
        )
    )


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_conversation(
    conversation_id: UUID,
    user: CurrentUser,
    db: DbSession,
) -> Response:
    delete_conversation(db, conversation_id, user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{conversation_id}/messages", response_model=list[MessageResponse])
def get_conversation_messages(
    conversation_id: UUID,
    user: CurrentUser,
    db: DbSession,
) -> list[MessageResponse]:
    conversation = get_owned_conversation(db, conversation_id, user)
    allowed_document_ids = (
        {
            association.document_id
            for association in conversation.document_associations
        }
        if conversation.chat_type == "knowledge"
        else set()
    )
    return [
        _message_response(
            db,
            conversation.workspace_id,
            message,
            allowed_document_ids,
        )
        for message in list_messages(db, conversation_id, user)
    ]
