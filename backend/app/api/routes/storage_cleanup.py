from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import CurrentAdmin, DbSession
from app.models.storage_cleanup import (
    StorageCleanupReplayAudit,
    StorageCleanupTask,
)
from app.services.knowledge_storage import validate_storage_key

router = APIRouter(prefix="/admin/storage-cleanup-tasks", tags=["admin"])
_ALLOWED_BACKENDS = {"local", "r2"}


@router.post(
    "/{task_id}/replay",
    status_code=status.HTTP_202_ACCEPTED,
)
def replay_storage_cleanup_task(
    task_id: UUID,
    operator: CurrentAdmin,
    db: DbSession,
) -> dict[str, str]:
    task = db.scalar(
        select(StorageCleanupTask).where(StorageCleanupTask.id == task_id)
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Cleanup task not found.")
    if task.status == "in_progress" and task.lease_token is not None:
        raise HTTPException(
            status_code=409,
            detail="The cleanup task is currently leased by a worker.",
        )
    if task.status != "permanently_failed":
        raise HTTPException(
            status_code=409,
            detail="Only permanently failed cleanup tasks can be replayed.",
        )
    if task.lease_token is not None or task.lease_expires_at is not None:
        raise HTTPException(
            status_code=409,
            detail="The cleanup task has an active lease.",
        )
    if task.storage_backend not in _ALLOWED_BACKENDS:
        raise HTTPException(
            status_code=409,
            detail="The cleanup task has an invalid storage backend.",
        )
    try:
        validate_storage_key(task.storage_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail="The cleanup task has an invalid storage key.",
        ) from exc

    previous_attempt_count = task.attempt_count
    previous_error_code = task.last_error_code
    now = datetime.now(timezone.utc)
    try:
        result = db.execute(
            update(StorageCleanupTask)
            .where(
                StorageCleanupTask.id == task.id,
                StorageCleanupTask.status == "permanently_failed",
                StorageCleanupTask.lease_token.is_(None),
                StorageCleanupTask.lease_expires_at.is_(None),
                StorageCleanupTask.storage_backend == task.storage_backend,
                StorageCleanupTask.storage_key == task.storage_key,
            )
            .values(
                status="pending",
                attempt_count=0,
                next_attempt_at=now,
                lease_token=None,
                lease_expires_at=None,
                last_error_code=None,
                completed_at=None,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="The cleanup task changed and cannot be replayed.",
            )
        db.add(
            StorageCleanupReplayAudit(
                task_id=task.id,
                workspace_id=task.workspace_id,
                document_id=task.document_id,
                operator_user_id=operator.id,
                storage_backend=task.storage_backend,
                storage_key=task.storage_key,
                previous_attempt_count=previous_attempt_count,
                previous_error_code=previous_error_code,
                replayed_at=now,
            )
        )
        db.commit()
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="The cleanup task could not be replayed.",
        ) from exc

    return {"status": "pending", "message": "Cleanup task replay was queued."}
