"""Persist explicitly interrupted streamed responses.

Revision ID: 0004_interrupted
Revises: 0003_streaming
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004_interrupted"
down_revision: Union[str, None] = "0003_streaming"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="completed",
            nullable=False,
        ),
    )
    with op.batch_alter_table("chat_generations") as batch_op:
        batch_op.drop_constraint("ck_chat_generation_status", type_="check")
        batch_op.create_check_constraint(
            "ck_chat_generation_status",
            "status IN ('processing', 'completed', 'cancelled')",
        )
        batch_op.add_column(sa.Column("user_message_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("assistant_message_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_chat_generations_user_message_id_messages",
            "messages",
            ["user_message_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_chat_generations_assistant_message_id_messages",
            "messages",
            ["assistant_message_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("chat_generations") as batch_op:
        batch_op.drop_constraint(
            "fk_chat_generations_assistant_message_id_messages", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "fk_chat_generations_user_message_id_messages", type_="foreignkey"
        )
        batch_op.drop_column("assistant_message_id")
        batch_op.drop_column("user_message_id")
        batch_op.drop_constraint("ck_chat_generation_status", type_="check")
        batch_op.create_check_constraint(
            "ck_chat_generation_status",
            "status IN ('processing', 'completed')",
        )
    op.drop_column("messages", "status")
