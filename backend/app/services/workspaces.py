from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership


def get_membership(
    db: Session, workspace_id: UUID, user_id: UUID
) -> WorkspaceMembership | None:
    return db.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user_id,
        )
    )


def require_workspace_member(
    db: Session, workspace_id: UUID, user: User, owner_only: bool = False
) -> WorkspaceMembership:
    membership = get_membership(db, workspace_id, user.id)
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found.",
        )
    if owner_only and membership.role != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Workspace owner access required.",
        )
    return membership


def create_workspace(db: Session, user: User, name: str) -> Workspace:
    workspace = Workspace(name=name)
    db.add(workspace)
    db.flush()
    db.add(
        WorkspaceMembership(
            workspace_id=workspace.id,
            user_id=user.id,
            role="owner",
        )
    )
    db.commit()
    db.refresh(workspace)
    return workspace


def list_workspaces(db: Session, user: User) -> list[tuple[Workspace, str]]:
    rows = db.execute(
        select(Workspace, WorkspaceMembership.role)
        .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
        .where(WorkspaceMembership.user_id == user.id)
        .order_by(Workspace.created_at, Workspace.id)
    )
    return list(rows)


def add_workspace_member(
    db: Session, workspace_id: UUID, email: str
) -> tuple[User, WorkspaceMembership]:
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    membership = get_membership(db, workspace_id, user.id)
    if membership is not None:
        raise HTTPException(status_code=409, detail="User is already a workspace member.")
    membership = WorkspaceMembership(
        workspace_id=workspace_id,
        user_id=user.id,
        role="member",
    )
    db.add(membership)
    db.commit()
    db.refresh(membership)
    return user, membership
