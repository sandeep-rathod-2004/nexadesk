import hashlib
import hmac
import secrets
import time
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
from app.services.performance import log_auth_stage

_REFRESH_ROTATION_GRACE_SECONDS = 30


def register_user(
    db: Session,
    email: str,
    password: str,
    workspace_name: str,
    full_name: str | None = None,
    request_id: str | None = None,
) -> User:
    password_hash_started_at = time.perf_counter()
    password_hash = hash_password(password)
    log_auth_stage(
        "registration_password_hash_completed",
        password_hash_started_at,
        request_id,
    )
    user = User(
        full_name=full_name,
        email=email.strip().lower(),
        password_hash=password_hash,
    )
    workspace = Workspace(name=workspace_name)
    flush_started_at = time.perf_counter()
    db.add_all([user, workspace])
    db.flush()
    log_auth_stage(
        "registration_user_workspace_flush_completed",
        flush_started_at,
        request_id,
    )
    membership_started_at = time.perf_counter()
    db.add(
        WorkspaceMembership(
            user_id=user.id,
            workspace_id=workspace.id,
            role="owner",
        )
    )
    log_auth_stage(
        "registration_membership_added_to_session",
        membership_started_at,
        request_id,
    )
    commit_started_at = time.perf_counter()
    db.commit()
    log_auth_stage(
        "registration_transaction_committed",
        commit_started_at,
        request_id,
    )
    refresh_started_at = time.perf_counter()
    db.refresh(user)
    log_auth_stage(
        "registration_user_refreshed",
        refresh_started_at,
        request_id,
    )
    return user


def authenticate_user(
    db: Session, email: str, password: str, request_id: str | None = None
) -> tuple[User | None, bool]:
    lookup_started_at = time.perf_counter()
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    log_auth_stage("user_lookup_completed", lookup_started_at, request_id)
    if user is None:
        verification_started_at = time.perf_counter()
        verify_unknown_user_password(password)
        log_auth_stage(
            "password_verification_completed",
            verification_started_at,
            request_id,
        )
        return None, False
    if not user.is_active:
        return None, True
    verification_started_at = time.perf_counter()
    password_valid = verify_password(password, user.password_hash)
    log_auth_stage(
        "password_verification_completed",
        verification_started_at,
        request_id,
    )
    if not password_valid:
        return None, True
    return user, True


def _refresh_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _add_refresh_token(
    db: Session,
    user_id: UUID,
    family_id: UUID,
    settings: Settings,
    token: str | None = None,
) -> str:
    token = token or secrets.token_urlsafe(48)
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


def _rotated_token(token: str, settings: Settings) -> str:
    # Deterministic derivation lets concurrent retries receive the same child
    # token without persisting its raw value.
    key = settings.jwt_secret_key.get_secret_value().encode("utf-8")
    message = b"nexadesk-refresh-rotation-v1:" + token.encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _grace_replacement(
    db: Session,
    token_record: RefreshToken,
    token: str,
    settings: Settings,
    now: datetime,
) -> tuple[User, str] | None:
    revoked_at = token_record.revoked_at
    if revoked_at is None:
        return None
    if revoked_at.tzinfo is None:
        revoked_at = revoked_at.replace(tzinfo=timezone.utc)
    expires_at = token_record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if (
        expires_at <= now
        or (now - revoked_at).total_seconds() > _REFRESH_ROTATION_GRACE_SECONDS
    ):
        return None

    replacement = _rotated_token(token, settings)
    replacement_record = db.scalar(
        select(RefreshToken).where(
            RefreshToken.token_hash == _refresh_token_hash(replacement),
            RefreshToken.family_id == token_record.family_id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now,
        )
    )
    if replacement_record is None:
        return None
    user = db.get(User, token_record.user_id)
    if user is None or not user.is_active:
        return None
    return user, replacement


def rotate_refresh_token(
    db: Session, token: str, settings: Settings | None = None
) -> tuple[User, str] | None:
    current_settings = settings or get_settings()
    now = datetime.now(timezone.utc)
    if db.get_bind().dialect.name == "sqlite":
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    token_record = db.scalar(
        select(RefreshToken)
        .where(RefreshToken.token_hash == _refresh_token_hash(token))
        .with_for_update()
    )
    if token_record is None:
        return None

    if token_record.revoked_at is not None:
        replacement = _grace_replacement(
            db, token_record, token, current_settings, now
        )
        if replacement is not None:
            return replacement
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
        db.refresh(token_record)
        replacement = _grace_replacement(
            db, token_record, token, current_settings, now
        )
        if replacement is not None:
            db.commit()
            return replacement
        _revoke_family(db, token_record.family_id, now)
        db.commit()
        return None

    replacement = _rotated_token(token, current_settings)
    replacement = _add_refresh_token(
        db, user.id, token_record.family_id, current_settings, replacement
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
