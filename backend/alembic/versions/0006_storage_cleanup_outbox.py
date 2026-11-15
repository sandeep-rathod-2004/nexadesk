"""Add the durable document storage cleanup outbox.

Revision ID: 0006_storage_cleanup_outbox
Revises: 0005_embedding_identity
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006_storage_cleanup_outbox"
down_revision: Union[str, None] = "0005_embedding_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "storage_cleanup_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("storage_backend", sa.String(length=20), nullable=False),
        sa.Column("storage_key", sa.String(length=80), nullable=False),
        sa.Column(
            "status",
            sa.String(length=30),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=40), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "storage_backend IN ('local', 'r2')",
            name="ck_storage_cleanup_backend",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'in_progress', 'completed', 'permanently_failed')",
            name="ck_storage_cleanup_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_storage_cleanup_attempt_count"
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR last_error_code IN "
            "('storage_unavailable', 'invalid_storage_key', "
            "'invalid_storage_backend', 'lease_expired')",
            name="ck_storage_cleanup_error_code",
        ),
        sa.CheckConstraint(
            "(status = 'in_progress' AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status != 'in_progress' AND lease_token IS NULL "
            "AND lease_expires_at IS NULL)",
            name="ck_storage_cleanup_lease_state",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_storage_cleanup_tasks_workspace_id",
        "storage_cleanup_tasks",
        ["workspace_id"],
    )
    op.create_index(
        "ix_storage_cleanup_tasks_document_id",
        "storage_cleanup_tasks",
        ["document_id"],
    )
    op.create_index(
        "ix_storage_cleanup_due",
        "storage_cleanup_tasks",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_storage_cleanup_lease_expiration",
        "storage_cleanup_tasks",
        ["status", "lease_expires_at"],
    )
    op.create_index(
        "uq_storage_cleanup_active_object",
        "storage_cleanup_tasks",
        ["workspace_id", "document_id", "storage_key"],
        unique=True,
        sqlite_where=sa.text("status != 'completed'"),
        postgresql_where=sa.text("status != 'completed'"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    unfinished_tasks = connection.execute(
        sa.text(
            "SELECT status, COUNT(*) "
            "FROM storage_cleanup_tasks "
            "WHERE status IN ('pending', 'in_progress', 'permanently_failed') "
            "GROUP BY status"
        )
    ).all()
    if unfinished_tasks:
        counts = ", ".join(
            f"{status}={count}" for status, count in sorted(unfinished_tasks)
        )
        raise RuntimeError(
            "Cannot downgrade storage cleanup outbox while unfinished cleanup "
            f"tasks exist ({counts}). Process or preserve these tasks before retrying."
        )

    # Completed rows represent confirmed deletions and may be discarded on downgrade.
    op.drop_index(
        "uq_storage_cleanup_active_object", table_name="storage_cleanup_tasks"
    )
    op.drop_index(
        "ix_storage_cleanup_lease_expiration", table_name="storage_cleanup_tasks"
    )
    op.drop_index("ix_storage_cleanup_due", table_name="storage_cleanup_tasks")
    op.drop_index(
        "ix_storage_cleanup_tasks_document_id", table_name="storage_cleanup_tasks"
    )
    op.drop_index(
        "ix_storage_cleanup_tasks_workspace_id", table_name="storage_cleanup_tasks"
    )
    op.drop_table("storage_cleanup_tasks")
