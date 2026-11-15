import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.security import (
    hash_password,
    verify_password,
    verify_unknown_user_password,
)
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership


def register_user(
    db: Session,
    email: str,
    password: str,
    workspace_name: str,
    full_name: str | None = None,
) -> User:
    user = User(
        full_name=full_name,
        email=email.strip().lower(),
        password_hash=hash_password(password),
    )
    workspace = Workspace(name=workspace_name)
    db.add_all([user, workspace])
    db.flush()
    db.add(
        WorkspaceMembership(
            user_id=user.id,
            workspace_id=workspace.id,
            role="owner",
        )
    )
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(
    db: Session, email: str, password: str
) -> tuple[User | None, bool]:
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None:
        verify_unknown_user_password(password)
        return None, False
    if not user.is_active or not verify_password(password, user.password_hash):
        return None, True
    return user, True


def _refresh_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _add_refresh_token(
    db: Session,
    user_id: UUID,
    family_id: UUID,
    settings: Settings,
) -> str:
    token = secrets.token_urlsafe(48)
    db.add(
        RefreshToken(
            user_id=user_id,
            family_id=family_id,
            token_hash=_refresh_token_hash(token),
            expires_at=datetime.now(timezone.utc)
            + timedelta(days=settings.jwt_refresh_token_days),
        )
    )
    return token


def create_refresh_token(
    db: Session, user: User, settings: Settings | None = None
) -> str:
    return _add_refresh_token(db, user.id, uuid4(), settings or get_settings())


def _revoke_family(db: Session, family_id: UUID, now: datetime) -> None:
    db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.family_id == family_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )


def rotate_refresh_token(
    db: Session, token: str, settings: Settings | None = None
) -> tuple[User, str] | None:
    current_settings = settings or get_settings()
    now = datetime.now(timezone.utc)
    token_record = db.scalar(
        select(RefreshToken).where(
            RefreshToken.token_hash == _refresh_token_hash(token)
        )
    )
    if token_record is None:
        return None

    if token_record.revoked_at is not None:
        _revoke_family(db, token_record.family_id, now)
        db.commit()
        return None

    user = db.get(User, token_record.user_id)
    if user is None or not user.is_active:
        _revoke_family(db, token_record.family_id, now)
        db.commit()
        return None

    claimed = db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.id == token_record.id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now,
        )
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        _revoke_family(db, token_record.family_id, now)
        db.commit()
        return None

    replacement = _add_refresh_token(
        db, user.id, token_record.family_id, current_settings
    )
    db.commit()
    return user, replacement


def revoke_refresh_token(db: Session, token: str) -> None:
    token_record = db.scalar(
        select(RefreshToken).where(
            RefreshToken.token_hash == _refresh_token_hash(token)
        )
    )
    if token_record is None:
        return
    _revoke_family(db, token_record.family_id, datetime.now(timezone.utc))
    db.commit()
