"""Add streamed chat request idempotency records.

Revision ID: 0003_streaming
Revises: 0002_knowledge
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003_streaming"
down_revision: Union[str, None] = "0002_knowledge"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_generations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("reply", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("grounded", sa.Boolean(), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
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
        sa.CheckConstraint(
            "status IN ('processing', 'completed')",
            name="ck_chat_generation_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "request_id", name="uq_chat_generation_request"),
    )
    op.create_index("ix_chat_generations_user_id", "chat_generations", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_chat_generations_user_id", table_name="chat_generations")
    op.drop_table("chat_generations")
