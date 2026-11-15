"""Record the storage backend for each knowledge document.

Revision ID: 0007_document_storage_backend
Revises: 0006_storage_cleanup_outbox
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007_document_storage_backend"
down_revision: Union[str, None] = "0006_storage_cleanup_outbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    with op.batch_alter_table("knowledge_documents") as batch_op:
        batch_op.add_column(sa.Column("storage_backend", sa.String(length=20)))
        batch_op.create_check_constraint(
            "ck_knowledge_document_storage_backend",
            "storage_backend IS NULL OR storage_backend IN ('local', 'r2')",
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
    backend_mappings = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM knowledge_documents "
            "WHERE storage_backend IS NOT NULL"
        )
    )
    if unfinished_tasks or backend_mappings:
        reasons = []
        if unfinished_tasks:
            counts = ", ".join(
                f"{status}={count}" for status, count in sorted(unfinished_tasks)
            )
            reasons.append(f"unfinished cleanup tasks exist ({counts})")
        if backend_mappings:
            reasons.append(f"{backend_mappings} document backend mappings exist")
        raise RuntimeError(
            "Cannot downgrade document storage backend while "
            + " and ".join(reasons)
            + ". Preserve or resolve this data before retrying."
        )

    with op.batch_alter_table("knowledge_documents") as batch_op:
        batch_op.drop_constraint(
            "ck_knowledge_document_storage_backend", type_="check"
        )
        batch_op.drop_column("storage_backend")
