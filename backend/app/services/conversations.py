from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.conversation import Conversation, ConversationDocument, Message
from app.models.knowledge import KnowledgeDocument
from app.models.user import User
from app.models.workspace import WorkspaceMembership
from app.services.workspaces import require_workspace_member


def get_owned_conversation(
    db: Session, conversation_id: UUID, user: User
) -> Conversation:
    conversation = db.scalar(
        select(Conversation)
        .options(selectinload(Conversation.document_associations))
        .join(
            WorkspaceMembership,
            WorkspaceMembership.workspace_id == Conversation.workspace_id,
        )
        .where(
            Conversation.id == conversation_id,
            WorkspaceMembership.user_id == user.id,
            Conversation.created_by_id == user.id,
        )
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return conversation


def list_owned_conversations(
    db: Session,
    user: User,
    workspace_id: UUID | None = None,
    chat_type: str | None = None,
) -> list[Conversation]:
    query = (
        select(Conversation)
        .options(selectinload(Conversation.document_associations))
        .join(
            WorkspaceMembership,
            WorkspaceMembership.workspace_id == Conversation.workspace_id,
        )
        .where(
            WorkspaceMembership.user_id == user.id,
            Conversation.created_by_id == user.id,
        )
        .order_by(Conversation.updated_at.desc(), Conversation.id)
    )
    if workspace_id is not None:
        require_workspace_member(db, workspace_id, user)
        query = query.where(Conversation.workspace_id == workspace_id)
    if chat_type is not None:
        query = query.where(Conversation.chat_type == chat_type)
    return list(db.scalars(query))


def _validate_document_ids(
    db: Session, workspace_id: UUID, document_ids: list[UUID]
) -> list[UUID]:
    if not document_ids or len(set(document_ids)) != len(document_ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Select one or more ready documents for this Knowledge Chat.",
        )
    documents = list(
        db.scalars(
            select(KnowledgeDocument).where(
                KnowledgeDocument.id.in_(document_ids),
                KnowledgeDocument.workspace_id == workspace_id,
                KnowledgeDocument.status == "ready",
            )
        )
    )
    if len(documents) != len(document_ids):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="One or more selected documents are unavailable.",
        )
    return document_ids


def create_conversation(
    db: Session,
    user: User,
    workspace_id: UUID,
    title: str,
    chat_type: str = "normal",
    document_ids: list[UUID] | None = None,
) -> Conversation:
    require_workspace_member(db, workspace_id, user)
    selected_documents = document_ids or []
    if chat_type == "normal" and selected_documents:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Normal Chats cannot have knowledge documents.",
        )
    if chat_type == "knowledge":
        selected_documents = _validate_document_ids(
            db, workspace_id, selected_documents
        )
    elif chat_type != "normal":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Unsupported conversation type.",
        )
    conversation = Conversation(
        workspace_id=workspace_id,
        created_by_id=user.id,
        title=title,
        chat_type=chat_type,
    )
    db.add(conversation)
    db.flush()
    conversation.document_associations = [
        ConversationDocument(
            document_id=document_id,
            workspace_id=workspace_id,
        )
        for document_id in selected_documents
    ]
    db.commit()
    db.refresh(conversation)
    return conversation


def update_conversation_documents(
    db: Session,
    conversation_id: UUID,
    user: User,
    document_ids: list[UUID],
) -> Conversation:
    conversation = get_owned_conversation(db, conversation_id, user)
    if conversation.chat_type != "knowledge":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only Knowledge Chats can have associated documents.",
        )
    selected_documents = _validate_document_ids(
        db, conversation.workspace_id, document_ids
    )
    selected_ids = set(selected_documents)
    existing_associations = conversation.document_associations
    existing_ids = {
        association.document_id for association in existing_associations
    }
    conversation.document_associations = [
        association
        for association in existing_associations
        if association.document_id in selected_ids
    ] + [
        ConversationDocument(
            document_id=document_id,
            workspace_id=conversation.workspace_id,
        )
        for document_id in selected_ids - existing_ids
    ]
    db.commit()
    db.refresh(conversation)
    return conversation


def rename_conversation(
    db: Session, conversation_id: UUID, user: User, title: str
) -> Conversation:
    conversation = get_owned_conversation(db, conversation_id, user)
    conversation.title = title
    db.commit()
    db.refresh(conversation)
    return conversation


def delete_conversation(db: Session, conversation_id: UUID, user: User) -> None:
    conversation = get_owned_conversation(db, conversation_id, user)
    db.delete(conversation)
    db.commit()


def list_messages(
    db: Session, conversation_id: UUID, user: User
) -> list[Message]:
    get_owned_conversation(db, conversation_id, user)
    return list(
        db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.sequence)
        )
    )


def resolve_workspace_id(
    db: Session, user: User, workspace_id: UUID | None
) -> UUID:
    if workspace_id is not None:
        require_workspace_member(db, workspace_id, user)
        return workspace_id
    memberships = list(
        db.scalars(
            select(WorkspaceMembership).where(WorkspaceMembership.user_id == user.id)
        )
    )
    if not memberships:
        raise HTTPException(status_code=403, detail="Workspace membership required.")
    if len(memberships) != 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Select a workspace for this conversation.",
        )
    return memberships[0].workspace_id
