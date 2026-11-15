from uuid import UUID

from fastapi import APIRouter, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.user import User
from app.models.workspace import WorkspaceMembership
from app.schemas.workspace import (
    WorkspaceCreate,
    WorkspaceMemberCreate,
    WorkspaceMemberResponse,
    WorkspaceResponse,
)
from app.services.workspaces import (
    add_workspace_member,
    create_workspace,
    list_workspaces,
    require_workspace_member,
)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.get("", response_model=list[WorkspaceResponse])
def get_workspaces(user: CurrentUser, db: DbSession) -> list[WorkspaceResponse]:
    return [
        WorkspaceResponse(id=workspace.id, name=workspace.name, role=role)
        for workspace, role in list_workspaces(db, user)
    ]


@router.post("", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
def post_workspace(
    body: WorkspaceCreate, user: CurrentUser, db: DbSession
) -> WorkspaceResponse:
    workspace = create_workspace(db, user, body.name)
    return WorkspaceResponse(id=workspace.id, name=workspace.name, role="owner")


@router.post(
    "/{workspace_id}/members",
    response_model=WorkspaceMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
def post_workspace_member(
    workspace_id: UUID,
    body: WorkspaceMemberCreate,
    user: CurrentUser,
    db: DbSession,
) -> WorkspaceMemberResponse:
    require_workspace_member(db, workspace_id, user, owner_only=True)
    member, membership = add_workspace_member(db, workspace_id, str(body.email))
    return WorkspaceMemberResponse(
        user_id=member.id,
        email=member.email,
        role=membership.role,
    )


@router.get("/{workspace_id}/members", response_model=list[WorkspaceMemberResponse])
def get_workspace_members(
    workspace_id: UUID,
    user: CurrentUser,
    db: DbSession,
) -> list[WorkspaceMemberResponse]:
    require_workspace_member(db, workspace_id, user)
    rows = db.execute(
        select(User, WorkspaceMembership.role)
        .join(WorkspaceMembership, WorkspaceMembership.user_id == User.id)
        .where(WorkspaceMembership.workspace_id == workspace_id)
        .order_by(User.email)
    )
    return [
        WorkspaceMemberResponse(user_id=member.id, email=member.email, role=role)
        for member, role in rows
    ]
