import time

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import DbSession
from app.core.config import get_settings
from app.core.security import create_access_token
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership
from app.schemas.auth import (
    AuthResponse,
    LoginRequest,
    RegistrationResponse,
    RegisterRequest,
    UserResponse,
    WorkspaceAccess,
)
from app.services.auth import (
    authenticate_user,
    create_refresh_token,
    register_user,
    revoke_refresh_token,
    rotate_refresh_token,
)
from app.services.performance import log_auth_stage

router = APIRouter(prefix="/auth", tags=["authentication"])
_REFRESH_COOKIE = "nexadesk_refresh"


def _workspace_accesses(
    db: Session, user: User, request_id: str | None = None
) -> list[WorkspaceAccess]:
    lookup_started_at = time.perf_counter()
    rows = db.execute(
        select(Workspace, WorkspaceMembership.role)
        .join(
            WorkspaceMembership,
            WorkspaceMembership.workspace_id == Workspace.id,
        )
        .where(WorkspaceMembership.user_id == user.id)
        .order_by(Workspace.created_at, Workspace.id)
    )
    workspaces = [
        WorkspaceAccess(id=workspace.id, name=workspace.name, role=role)
        for workspace, role in rows
    ]
    log_auth_stage(
        "workspace_memberships_lookup_completed",
        lookup_started_at,
        request_id,
    )
    return workspaces


def _auth_response(
    db: Session, user: User, request_id: str | None = None
) -> AuthResponse:
    return AuthResponse(
        access_token=create_access_token(user.id),
        user=UserResponse(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
        ),
        workspaces=_workspace_accesses(db, user, request_id),
    )


def _set_refresh_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=token,
        max_age=settings.jwt_refresh_token_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.app_env == "production",
        samesite="none" if settings.app_env == "production" else "lax",
        path=f"{settings.api_v1_prefix.rstrip('/')}/auth",
    )


def _clear_refresh_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        key=_REFRESH_COOKIE,
        httponly=True,
        secure=settings.app_env == "production",
        samesite="none" if settings.app_env == "production" else "lax",
        path=f"{settings.api_v1_prefix.rstrip('/')}/auth",
    )


def _validate_refresh_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is not None and origin not in get_settings().allowed_origins:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Request origin is not allowed.",
        )


@router.post(
    "/register",
    response_model=RegistrationResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(body: RegisterRequest, db: DbSession) -> RegistrationResponse:
    try:
        user = register_user(
            db,
            str(body.email),
            body.password.get_secret_value(),
            body.workspace_name,
            body.full_name,
        )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        ) from exc
    return RegistrationResponse(
        user=UserResponse(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
        ),
        workspaces=_workspace_accesses(db, user),
    )


@router.post("/login", response_model=AuthResponse)
def login(
    body: LoginRequest, request: Request, response: Response, db: DbSession
) -> AuthResponse:
    request_id = getattr(request.state, "auth_request_id", None)
    user, email_registered = authenticate_user(
        db,
        str(body.email),
        body.password.get_secret_value(),
        request_id=request_id,
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Incorrect email or password."
                if email_registered
                else "This email is not registered. Please register first."
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )
    token_started_at = time.perf_counter()
    refresh_token = create_refresh_token(db, user)
    log_auth_stage("refresh_token_created", token_started_at, request_id)
    commit_started_at = time.perf_counter()
    db.commit()
    log_auth_stage("refresh_token_committed", commit_started_at, request_id)
    _set_refresh_cookie(response, refresh_token)
    return _auth_response(db, user, request_id)


@router.post("/refresh", response_model=AuthResponse)
def refresh(
    request: Request,
    response: Response,
    db: DbSession,
) -> AuthResponse:
    _validate_refresh_origin(request)
    token = request.cookies.get(_REFRESH_COOKIE)
    if token is None:
        _clear_refresh_cookie(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session refresh is invalid. Please sign in again.",
        )

    rotated = rotate_refresh_token(db, token)
    if rotated is None:
        _clear_refresh_cookie(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session refresh is invalid. Please sign in again.",
        )

    user, new_token = rotated
    _set_refresh_cookie(response, new_token)
    return _auth_response(db, user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: DbSession) -> Response:
    _validate_refresh_origin(request)
    token = request.cookies.get(_REFRESH_COOKIE)
    if token is not None:
        revoke_refresh_token(db, token)
    _clear_refresh_cookie(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
