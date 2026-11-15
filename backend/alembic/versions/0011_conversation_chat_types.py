"""Separate normal and knowledge chats and associate knowledge documents.

Revision ID: 0011_chat_types
Revises: 0010_refresh_tokens
"""

import json
from typing import Sequence, Union
from uuid import UUID

from alembic import op
import sqlalchemy as sa

revision: str = "0011_chat_types"
down_revision: Union[str, None] = "0010_refresh_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _source_ids(sources: object) -> list[tuple[UUID, UUID]]:
    if isinstance(sources, str):
        try:
            sources = json.loads(sources)
        except json.JSONDecodeError:
            return []
    if not isinstance(sources, list):
        return []

    result: list[tuple[UUID, UUID]] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        document_id = _uuid(source.get("document_id"))
        chunk_id = _uuid(source.get("chunk_id") or source.get("id"))
        if document_id is not None and chunk_id is not None:
            result.append((document_id, chunk_id))
    return result


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "chat_type",
            sa.String(length=20),
            server_default="normal",
            nullable=False,
        ),
    )
    op.create_index(
        "uq_conversation_id_workspace",
        "conversations",
        ["id", "workspace_id"],
        unique=True,
    )
    op.create_table(
        "conversation_documents",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "workspace_id"],
            ["conversations.id", "conversations.workspace_id"],
            name="fk_conversation_document_conversation_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["knowledge_documents.id", "knowledge_documents.workspace_id"],
            name="fk_conversation_document_document_workspace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("conversation_id", "document_id"),
    )
    op.create_index(
        "ix_conversation_documents_workspace_id",
        "conversation_documents",
        ["workspace_id"],
    )

    connection = op.get_bind()
    conversations = sa.table(
        "conversations",
        sa.column("id", sa.Uuid()),
        sa.column("workspace_id", sa.Uuid()),
        sa.column("chat_type", sa.String()),
    )
    messages = sa.table(
        "messages",
        sa.column("conversation_id", sa.Uuid()),
        sa.column("sources", sa.JSON()),
    )
    chunks = sa.table(
        "knowledge_chunks",
        sa.column("id", sa.Uuid()),
        sa.column("document_id", sa.Uuid()),
        sa.column("workspace_id", sa.Uuid()),
    )

    conversation_workspaces = {
        row.id: row.workspace_id
        for row in connection.execute(
            sa.select(conversations.c.id, conversations.c.workspace_id)
        )
    }
    candidates: set[tuple[UUID, UUID, UUID, UUID]] = set()
    for row in connection.execute(
        sa.select(messages.c.conversation_id, messages.c.sources)
    ):
        conversation_id = _uuid(row.conversation_id)
        workspace_id = conversation_workspaces.get(conversation_id)
        if conversation_id is None or workspace_id is None:
            continue
        for document_id, chunk_id in _source_ids(row.sources):
            candidates.add((conversation_id, workspace_id, document_id, chunk_id))

    verified: set[tuple[UUID, UUID, UUID]] = set()
    candidate_chunk_ids = {candidate[3] for candidate in candidates}
    chunk_documents: dict[UUID, tuple[UUID, UUID]] = {}
    chunk_id_list = list(candidate_chunk_ids)
    for start in range(0, len(chunk_id_list), 500):
        batch = chunk_id_list[start : start + 500]
        if not batch:
            continue
        for chunk in connection.execute(
            sa.select(chunks.c.id, chunks.c.document_id, chunks.c.workspace_id).where(
                chunks.c.id.in_(batch)
            )
        ):
            chunk_documents[chunk.id] = (chunk.document_id, chunk.workspace_id)

    for conversation_id, workspace_id, document_id, chunk_id in candidates:
        if chunk_documents.get(chunk_id) == (document_id, workspace_id):
            verified.add((conversation_id, document_id, workspace_id))

    knowledge_conversation_ids = {item[0] for item in verified}
    for conversation_id in knowledge_conversation_ids:
        connection.execute(
            conversations.update()
            .where(conversations.c.id == conversation_id)
            .values(chat_type="knowledge")
        )
    association = sa.table(
        "conversation_documents",
        sa.column("conversation_id", sa.Uuid()),
        sa.column("document_id", sa.Uuid()),
        sa.column("workspace_id", sa.Uuid()),
    )
    if verified:
        connection.execute(
            association.insert(),
            [
                {
                    "conversation_id": conversation_id,
                    "document_id": document_id,
                    "workspace_id": workspace_id,
                }
                for conversation_id, document_id, workspace_id in verified
            ],
        )


def downgrade() -> None:
    connection = op.get_bind()
    knowledge_chats = connection.scalar(
        sa.text("SELECT COUNT(*) FROM conversations WHERE chat_type = 'knowledge'")
    )
    associations = connection.scalar(
        sa.text("SELECT COUNT(*) FROM conversation_documents")
    )
    if knowledge_chats or associations:
        raise RuntimeError(
            "Cannot downgrade while Knowledge Chats or their document associations exist."
        )
    op.drop_index(
        "ix_conversation_documents_workspace_id",
        table_name="conversation_documents",
    )
    op.drop_table("conversation_documents")
    op.drop_index(
        "uq_conversation_id_workspace",
        table_name="conversations",
    )
    op.drop_column("conversations", "chat_type")
