"""Add an optional full name to users.

Revision ID: 0009_user_full_name
Revises: 0008_cleanup_replay_audit
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0009_user_full_name"
down_revision: Union[str, None] = "0008_cleanup_replay_audit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("full_name", sa.String(length=120), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    unfinished_tasks = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM storage_cleanup_tasks "
            "WHERE status IN ('pending', 'in_progress', 'permanently_failed')"
        )
    )
    if unfinished_tasks:
        raise RuntimeError(
            "Cannot downgrade while unfinished cleanup tasks exist; "
            "preserve the storage cleanup outbox."
        )

    backend_mappings = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM knowledge_documents "
            "WHERE storage_backend IS NOT NULL"
        )
    )
    if backend_mappings:
        raise RuntimeError(
            "Cannot downgrade while document backend mappings exist; "
            "preserve recorded storage backends."
        )

    replay_audits = connection.scalar(
        sa.text("SELECT COUNT(*) FROM storage_cleanup_replay_audits")
    )
    if replay_audits:
        raise RuntimeError(
            "Cannot downgrade while replay audit records exist; "
            "preserve cleanup replay history."
        )

    names = connection.scalar(
        sa.text("SELECT COUNT(*) FROM users WHERE full_name IS NOT NULL")
    )
    if names:
        raise RuntimeError(
            "Cannot downgrade while user full-name data exists; "
            "preserve account profile data."
        )

    op.drop_column("users", "full_name")
