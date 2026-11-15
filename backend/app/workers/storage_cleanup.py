import argparse
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.db.session import get_session_factory
from app.models.storage_cleanup import StorageCleanupTask
from app.services.knowledge_storage import (
    DocumentStorageError,
    DocumentStorageNotFoundError,
    get_document_storage_for_backend,
    validate_storage_key,
)

logger = logging.getLogger(__name__)
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_LEASE_SECONDS = 120
BASE_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 3600


@dataclass(frozen=True)
class CleanupClaim:
    task_id: UUID
    workspace_id: UUID
    document_id: UUID
    storage_backend: str
    storage_key: str
    attempt_count: int
    lease_token: UUID


def _due_or_expired(now: datetime):
    return or_(
        and_(
            StorageCleanupTask.status == "pending",
            StorageCleanupTask.next_attempt_at <= now,
        ),
        and_(
            StorageCleanupTask.status == "in_progress",
            StorageCleanupTask.lease_expires_at <= now,
        ),
    )


def _expire_exhausted_tasks(db: Session, now: datetime, max_attempts: int) -> None:
    exhausted = and_(
        _due_or_expired(now),
        StorageCleanupTask.attempt_count >= max_attempts,
    )
    db.execute(
        update(StorageCleanupTask)
        .where(exhausted)
        .values(
            status="permanently_failed",
            lease_token=None,
            lease_expires_at=None,
            last_error_code="lease_expired",
            updated_at=now,
        )
    )


def claim_cleanup_task(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: UUID | None = None,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> CleanupClaim | None:
    current_time = now or datetime.now(timezone.utc)
    lease_token = uuid4()
    lease_expires_at = current_time + timedelta(seconds=lease_seconds)
    with session_factory() as db:
        if workspace_id is None:
            _expire_exhausted_tasks(db, current_time, max_attempts)
        else:
            exhausted = and_(
                _due_or_expired(current_time),
                StorageCleanupTask.attempt_count >= max_attempts,
                StorageCleanupTask.workspace_id == workspace_id,
            )
            db.execute(
                update(StorageCleanupTask)
                .where(exhausted)
                .values(
                    status="permanently_failed",
                    lease_token=None,
                    lease_expires_at=None,
                    last_error_code="lease_expired",
                    updated_at=current_time,
                )
            )
        eligible = and_(
            _due_or_expired(current_time),
            StorageCleanupTask.attempt_count < max_attempts,
            *(
                (StorageCleanupTask.workspace_id == workspace_id,)
                if workspace_id is not None
                else ()
            ),
        )
        dialect = db.get_bind().dialect.name
        if dialect == "postgresql":
            task = db.scalar(
                select(StorageCleanupTask)
                .where(eligible)
                .order_by(StorageCleanupTask.created_at, StorageCleanupTask.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if task is None:
                db.commit()
                return None
            task.status = "in_progress"
            task.attempt_count += 1
            task.lease_token = lease_token
            task.lease_expires_at = lease_expires_at
            task.last_error_code = None
            task.updated_at = current_time
            claim = CleanupClaim(
                task.id,
                task.workspace_id,
                task.document_id,
                task.storage_backend,
                task.storage_key,
                task.attempt_count,
                lease_token,
            )
            db.commit()
            return claim

        selected_id = (
            select(StorageCleanupTask.id)
            .where(eligible)
            .order_by(StorageCleanupTask.created_at, StorageCleanupTask.id)
            .limit(1)
            .scalar_subquery()
        )
        result = db.execute(
            update(StorageCleanupTask)
            .where(StorageCleanupTask.id == selected_id, eligible)
            .values(
                status="in_progress",
                attempt_count=StorageCleanupTask.attempt_count + 1,
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
                last_error_code=None,
                updated_at=current_time,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            db.commit()
            return None
        task = db.scalar(
            select(StorageCleanupTask).where(
                StorageCleanupTask.lease_token == lease_token
            )
        )
        if task is None:
            db.rollback()
            raise RuntimeError("Claimed cleanup task could not be loaded.")
        claim = CleanupClaim(
            task.id,
            task.workspace_id,
            task.document_id,
            task.storage_backend,
            task.storage_key,
            task.attempt_count,
            lease_token,
        )
        db.commit()
        return claim


def _backoff_seconds(attempt_count: int) -> float:
    ceiling = min(
        MAX_BACKOFF_SECONDS,
        BASE_BACKOFF_SECONDS * (2 ** min(max(0, attempt_count - 1), 10)),
    )
    return random.uniform(0, ceiling)


def _finish_success(
    session_factory: sessionmaker[Session], claim: CleanupClaim, now: datetime
) -> bool:
    with session_factory() as db:
        result = db.execute(
            update(StorageCleanupTask)
            .where(
                StorageCleanupTask.id == claim.task_id,
                StorageCleanupTask.status == "in_progress",
                StorageCleanupTask.lease_token == claim.lease_token,
            )
            .values(
                status="completed",
                lease_token=None,
                lease_expires_at=None,
                last_error_code=None,
                completed_at=now,
                updated_at=now,
            )
        )
        db.commit()
        return result.rowcount == 1


def _finish_failure(
    session_factory: sessionmaker[Session],
    claim: CleanupClaim,
    *,
    now: datetime,
    error_code: str,
    max_attempts: int,
    permanent: bool = False,
) -> str:
    permanently_failed = permanent or claim.attempt_count >= max_attempts
    next_attempt = now + timedelta(seconds=_backoff_seconds(claim.attempt_count))
    with session_factory() as db:
        result = db.execute(
            update(StorageCleanupTask)
            .where(
                StorageCleanupTask.id == claim.task_id,
                StorageCleanupTask.status == "in_progress",
                StorageCleanupTask.lease_token == claim.lease_token,
            )
            .values(
                status="permanently_failed" if permanently_failed else "pending",
                next_attempt_at=next_attempt,
                lease_token=None,
                lease_expires_at=None,
                last_error_code=error_code,
                updated_at=now,
            )
        )
        db.commit()
        if result.rowcount != 1:
            return "lease_lost"
    return "permanently_failed" if permanently_failed else "retry_scheduled"


def process_next_cleanup_task(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    storage_factory=get_document_storage_for_backend,
) -> str:
    current_time = now or datetime.now(timezone.utc)
    claim = claim_cleanup_task(
        session_factory,
        now=current_time,
        lease_seconds=lease_seconds,
        max_attempts=max_attempts,
    )
    if claim is None:
        return "idle"

    try:
        validate_storage_key(claim.storage_key)
    except ValueError:
        return _finish_failure(
            session_factory,
            claim,
            now=current_time,
            error_code="invalid_storage_key",
            max_attempts=max_attempts,
            permanent=True,
        )
    if claim.storage_backend not in {"local", "r2"}:
        return _finish_failure(
            session_factory,
            claim,
            now=current_time,
            error_code="invalid_storage_backend",
            max_attempts=max_attempts,
            permanent=True,
        )

    try:
        storage = storage_factory(settings, claim.storage_backend)
        if storage.exists(settings, claim.workspace_id, claim.storage_key):
            storage.delete(settings, claim.workspace_id, claim.storage_key)
            if storage.exists(settings, claim.workspace_id, claim.storage_key):
                raise DocumentStorageError("Object remains after deletion.")
    except DocumentStorageNotFoundError:
        pass
    except (DocumentStorageError, OSError):
        logger.warning("Storage cleanup attempt failed.")
        return _finish_failure(
            session_factory,
            claim,
            now=current_time,
            error_code="storage_unavailable",
            max_attempts=max_attempts,
        )

    if not _finish_success(session_factory, claim, current_time):
        return "lease_lost"
    return "completed"


def run_worker(
    *,
    once: bool = False,
    poll_seconds: float = 5.0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> None:
    settings = get_settings()
    session_factory = get_session_factory()
    while True:
        outcome = process_next_cleanup_task(
            session_factory,
            settings,
            max_attempts=max_attempts,
            lease_seconds=lease_seconds,
        )
        if once:
            return
        if outcome == "idle":
            time.sleep(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Process document storage cleanup tasks.")
    parser.add_argument("--once", action="store_true", help="Process at most one task.")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument("--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS)
    args = parser.parse_args()
    if args.poll_seconds <= 0 or args.max_attempts < 1 or args.lease_seconds < 1:
        parser.error("poll, attempt, and lease values must be positive.")
    run_worker(
        once=args.once,
        poll_seconds=args.poll_seconds,
        max_attempts=args.max_attempts,
        lease_seconds=args.lease_seconds,
    )


if __name__ == "__main__":
    main()
