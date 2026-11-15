from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StorageCleanupTask(Base):
    __tablename__ = "storage_cleanup_tasks"
    __table_args__ = (
        CheckConstraint(
            "storage_backend IN ('local', 'r2')",
            name="ck_storage_cleanup_backend",
        ),
        CheckConstraint(
            "status IN ('pending', 'in_progress', 'completed', 'permanently_failed')",
            name="ck_storage_cleanup_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_storage_cleanup_attempt_count"),
        CheckConstraint(
            "last_error_code IS NULL OR last_error_code IN "
            "('storage_unavailable', 'invalid_storage_key', "
            "'invalid_storage_backend', 'lease_expired')",
            name="ck_storage_cleanup_error_code",
        ),
        CheckConstraint(
            "(status = 'in_progress' AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status != 'in_progress' AND lease_token IS NULL "
            "AND lease_expires_at IS NULL)",
            name="ck_storage_cleanup_lease_state",
        ),
        Index(
            "ix_storage_cleanup_due",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_storage_cleanup_lease_expiration",
            "status",
            "lease_expires_at",
        ),
        Index(
            "uq_storage_cleanup_active_object",
            "workspace_id",
            "document_id",
            "storage_key",
            unique=True,
            sqlite_where=text("status != 'completed'"),
            postgresql_where=text("status != 'completed'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    document_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    storage_backend: Mapped[str] = mapped_column(String(20), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending", server_default=text("'pending'")
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_token: Mapped[UUID | None] = mapped_column(Uuid)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StorageCleanupReplayAudit(Base):
    __tablename__ = "storage_cleanup_replay_audits"
    __table_args__ = (
        CheckConstraint(
            "previous_error_code IS NULL OR previous_error_code IN "
            "('storage_unavailable', 'invalid_storage_key', "
            "'invalid_storage_backend', 'lease_expired')",
            name="ck_storage_cleanup_replay_error_code",
        ),
        CheckConstraint(
            "previous_attempt_count >= 0",
            name="ck_storage_cleanup_replay_attempt_count",
        ),
        Index("ix_storage_cleanup_replay_task", "task_id", "replayed_at"),
        Index("ix_storage_cleanup_replay_operator", "operator_user_id", "replayed_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    document_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    operator_user_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(20), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(80), nullable=False)
    previous_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_error_code: Mapped[str | None] = mapped_column(String(40))
    replayed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
