import importlib.util
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _migration_module():
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "0011_conversation_chat_types.py"
    )
    spec = importlib.util.spec_from_file_location(
        "chat_type_migration", migration_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_schema(connection):
    metadata = sa.MetaData()
    sa.Table("workspaces", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    sa.Table(
        "knowledge_documents",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.UniqueConstraint("id", "workspace_id"),
    )
    conversations = sa.Table(
        "conversations",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.UniqueConstraint("id", "workspace_id"),
    )
    messages = sa.Table(
        "messages",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
    )
    chunks = sa.Table(
        "knowledge_chunks",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
    )
    metadata.create_all(connection)
    return conversations, messages, chunks


def test_upgrade_classifies_only_verifiable_cited_documents_and_guards_downgrade():
    migration = _migration_module()
    engine = sa.create_engine("sqlite://")
    workspace_id = uuid4()
    conversation_id = uuid4()
    normal_conversation_id = uuid4()
    document_id = uuid4()
    chunk_id = uuid4()
    with engine.begin() as connection:
        conversations, messages, chunks = _legacy_schema(connection)
        connection.execute(
            conversations.insert(),
            [
                {
                    "id": conversation_id,
                    "workspace_id": workspace_id,
                    "title": "Cited history",
                },
                {
                    "id": normal_conversation_id,
                    "workspace_id": workspace_id,
                    "title": "Uncited history",
                },
            ],
        )
        connection.execute(
            chunks.insert(),
            {
                "id": chunk_id,
                "document_id": document_id,
                "workspace_id": workspace_id,
            },
        )
        connection.execute(
            messages.insert(),
            [
                {
                    "id": uuid4(),
                    "conversation_id": conversation_id,
                    "sources": [
                        {
                            "document_id": str(document_id),
                            "chunk_id": str(chunk_id),
                        }
                    ],
                },
                {
                    "id": uuid4(),
                    "conversation_id": normal_conversation_id,
                    "sources": [],
                },
            ],
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()

        updated_conversations = sa.Table(
            "conversations", sa.MetaData(), autoload_with=connection
        )
        associations = sa.Table(
            "conversation_documents", sa.MetaData(), autoload_with=connection
        )
        assert connection.scalar(
            sa.select(updated_conversations.c.chat_type).where(
                updated_conversations.c.id == conversation_id
            )
        ) == "knowledge"
        assert connection.scalar(
            sa.select(updated_conversations.c.chat_type).where(
                updated_conversations.c.id == normal_conversation_id
            )
        ) == "normal"
        association_rows = connection.execute(
            sa.select(
                associations.c.conversation_id,
                associations.c.document_id,
                associations.c.workspace_id,
            )
        ).all()
        assert [
            tuple(UUID(value) for value in row) for row in association_rows
        ] == [(conversation_id, document_id, workspace_id)]

        with Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(RuntimeError, match="Knowledge Chats"):
                migration.downgrade()
        assert connection.scalar(
            sa.select(sa.func.count()).select_from(associations)
        ) == 1
    engine.dispose()


def test_downgrade_succeeds_when_no_knowledge_chat_data_would_be_lost():
    migration = _migration_module()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        conversations, _, _ = _legacy_schema(connection)
        connection.execute(
            conversations.insert(),
            {
                "id": uuid4(),
                "workspace_id": uuid4(),
                "title": "Normal history",
            },
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.downgrade()
        assert "chat_type" not in {
            column["name"]
            for column in sa.inspect(connection).get_columns("conversations")
        }
        assert "conversation_documents" not in sa.inspect(connection).get_table_names()
    engine.dispose()
