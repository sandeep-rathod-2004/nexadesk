"""Track embedding identity for each knowledge document.

Revision ID: 0005_embedding_identity
Revises: 0004_interrupted
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0005_embedding_identity"
down_revision: Union[str, None] = "0004_interrupted"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "knowledge_documents",
        sa.Column("embedding_provider", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "knowledge_documents",
        sa.Column("embedding_model", sa.String(length=150), nullable=True),
    )
    op.add_column(
        "knowledge_documents",
        sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("knowledge_documents", "embedding_dimensions")
    op.drop_column("knowledge_documents", "embedding_model")
    op.drop_column("knowledge_documents", "embedding_provider")
