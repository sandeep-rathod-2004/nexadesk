from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.conversation import Conversation, Message
from app.models.user import User
from app.models.workspace import WorkspaceMembership
from app.services.workspaces import require_workspace_member


def get_owned_conversation(
    db: Session, conversation_id: UUID, user: User
) -> Conversation:
    conversation = db.scalar(
        select(Conversation)
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
    db: Session, user: User, workspace_id: UUID | None = None
) -> list[Conversation]:
    query = (
        select(Conversation)
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
    return list(db.scalars(query))


def create_conversation(
    db: Session, user: User, workspace_id: UUID, title: str
) -> Conversation:
    require_workspace_member(db, workspace_id, user)
    conversation = Conversation(
        workspace_id=workspace_id,
        created_by_id=user.id,
        title=title,
    )
    db.add(conversation)
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
