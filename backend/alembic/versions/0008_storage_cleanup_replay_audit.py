"""Add operator authorization and cleanup replay audit history.

Revision ID: 0008_cleanup_replay_audit
Revises: 0007_document_storage_backend
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0008_cleanup_replay_audit"
down_revision: Union[str, None] = "0007_document_storage_backend"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_admin",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )

    op.create_table(
        "storage_cleanup_replay_audits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("operator_user_id", sa.Uuid(), nullable=False),
        sa.Column("storage_backend", sa.String(length=20), nullable=False),
        sa.Column("storage_key", sa.String(length=80), nullable=False),
        sa.Column("previous_attempt_count", sa.Integer(), nullable=False),
        sa.Column("previous_error_code", sa.String(length=40), nullable=True),
        sa.Column(
            "replayed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "previous_error_code IS NULL OR previous_error_code IN "
            "('storage_unavailable', 'invalid_storage_key', "
            "'invalid_storage_backend', 'lease_expired')",
            name="ck_storage_cleanup_replay_error_code",
        ),
        sa.CheckConstraint(
            "previous_attempt_count >= 0",
            name="ck_storage_cleanup_replay_attempt_count",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_storage_cleanup_replay_task",
        "storage_cleanup_replay_audits",
        ["task_id", "replayed_at"],
    )
    op.create_index(
        "ix_storage_cleanup_replay_operator",
        "storage_cleanup_replay_audits",
        ["operator_user_id", "replayed_at"],
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
    replay_audits = connection.scalar(
        sa.text("SELECT COUNT(*) FROM storage_cleanup_replay_audits")
    )
    backend_mappings = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM knowledge_documents "
            "WHERE storage_backend IS NOT NULL"
        )
    )
    admin_users = connection.scalar(
        sa.text("SELECT COUNT(*) FROM users WHERE is_admin = :is_admin").bindparams(
            sa.bindparam("is_admin", True, type_=sa.Boolean())
        )
    )
    if unfinished_tasks or replay_audits or backend_mappings or admin_users:
        reasons = []
        if unfinished_tasks:
            counts = ", ".join(
                f"{status}={count}" for status, count in sorted(unfinished_tasks)
            )
            reasons.append(f"unfinished cleanup tasks exist ({counts})")
        if replay_audits:
            reasons.append(f"{replay_audits} replay audit records exist")
        if backend_mappings:
            reasons.append(f"{backend_mappings} document backend mappings exist")
        if admin_users:
            reasons.append(f"{admin_users} admin user records exist")
        raise RuntimeError(
            "Cannot downgrade storage cleanup replay audit while "
            + " and ".join(reasons)
            + ". Preserve or resolve this data before retrying."
        )

    op.drop_index(
        "ix_storage_cleanup_replay_operator",
        table_name="storage_cleanup_replay_audits",
    )
    op.drop_index(
        "ix_storage_cleanup_replay_task",
        table_name="storage_cleanup_replay_audits",
    )
    op.drop_table("storage_cleanup_replay_audits")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("is_admin")
