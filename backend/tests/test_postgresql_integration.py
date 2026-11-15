import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import BytesIO
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.engine import make_url

from app.api.deps import get_db
from app.core.config import Settings
from app.db.session import _engine_options
from app.main import app, fastapi_app
from app.models import (
    Conversation,
    KnowledgeChunk,
    KnowledgeDocument,
    Message,
    User,
    Workspace,
    WorkspaceMembership,
    StorageCleanupTask,
)
from app.services.knowledge_storage import LocalFilesystemDocumentStorage
from app.services.retrieval import retrieve_chunks
from app.services.storage_migration import transfer_local_documents_to_r2
from app.workers.storage_cleanup import claim_cleanup_task

TEST_DATABASE_NAME = "nexadesk_m2_test"
TEST_PASSWORD = "temporary-integration-password"


def _test_database_url() -> str:
    value = os.getenv("TEST_POSTGRESQL_URL")
    if not value:
        pytest.skip("TEST_POSTGRESQL_URL is not configured.")
    url = make_url(value)
    if (
        url.get_backend_name() != "postgresql"
        or url.get_driver_name() != "psycopg"
        or url.database != TEST_DATABASE_NAME
        or url.host not in {"localhost", "127.0.0.1", "::1"}
    ):
        pytest.fail(
            "PostgreSQL integration tests require a loopback psycopg URL "
            f"for the dedicated {TEST_DATABASE_NAME} database."
        )
    return value


@pytest.fixture(scope="module")
def postgres_session_factory():
    database_url = _test_database_url()
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        **_engine_options(database_url),
    )
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT 1")) == 1
            assert "PostgreSQL" in connection.scalar(text("SELECT version()"))
        yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    finally:
        engine.dispose()


@pytest.fixture
def postgres_client(postgres_session_factory):
    def override_get_db():
        db = postgres_session_factory()
        try:
            yield db
        finally:
            db.close()

    fastapi_app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        fastapi_app.dependency_overrides.pop(get_db, None)


def _register(client: TestClient, label: str) -> tuple[str, str, str]:
    suffix = uuid4().hex
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"{label}-{suffix}@example.com",
            "password": TEST_PASSWORD,
            "workspace_name": f"{label} workspace",
        },
    )
    assert response.status_code == 201
    payload = response.json()
    login = client.post(
        "/api/v1/auth/login",
        json={"email": payload["user"]["email"], "password": TEST_PASSWORD},
    )
    assert login.status_code == 200
    return (
        login.json()["access_token"],
        payload["user"]["id"],
        payload["workspaces"][0]["id"],
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_postgresql_migration_history_and_schema(postgres_session_factory):
    with postgres_session_factory() as db:
        version = db.scalar(text("SELECT version_num FROM alembic_version"))
        assert version == "0011_chat_types"
    engine = postgres_session_factory.kw["bind"]
    inspector = inspect(engine)
    document_columns = {
        column["name"] for column in inspector.get_columns("knowledge_documents")
    }
    assert {
        "embedding_provider",
        "embedding_model",
        "embedding_dimensions",
        "storage_backend",
    }.issubset(document_columns)
    assert "refresh_tokens" in inspector.get_table_names()
    document_constraints = inspector.get_check_constraints("knowledge_documents")
    assert "ck_knowledge_document_storage_backend" in {
        constraint["name"] for constraint in document_constraints
    }
    assert {
        "users",
        "workspaces",
        "workspace_memberships",
        "conversations",
        "messages",
        "knowledge_documents",
        "knowledge_chunks",
        "chat_generations",
        "storage_cleanup_tasks",
        "storage_cleanup_replay_audits",
        "alembic_version",
    }.issubset(set(inspector.get_table_names()))
    assert "is_admin" in {
        column["name"] for column in inspector.get_columns("users")
    }
    assert "full_name" in {
        column["name"] for column in inspector.get_columns("users")
    }

    membership_constraints = inspector.get_unique_constraints("workspace_memberships")
    assert any(
        constraint["name"] == "uq_workspace_membership"
        for constraint in membership_constraints
    )
    message_constraints = inspector.get_unique_constraints("messages")
    assert any(
        constraint["name"] == "uq_message_conversation_sequence"
        for constraint in message_constraints
    )
    assert "ix_knowledge_chunks_workspace_id" in {
        item["name"] for item in inspector.get_indexes("knowledge_chunks")
    }
    cleanup_constraints = inspector.get_check_constraints("storage_cleanup_tasks")
    assert {
        "ck_storage_cleanup_backend",
        "ck_storage_cleanup_status",
        "ck_storage_cleanup_attempt_count",
        "ck_storage_cleanup_error_code",
        "ck_storage_cleanup_lease_state",
    }.issubset({constraint["name"] for constraint in cleanup_constraints})


def test_postgresql_concurrent_cleanup_claims_are_distinct(postgres_session_factory):
    workspace_id = UUID("a610d2c0-2905-4430-b67c-17e72dcb5221")
    task_ids = [uuid4(), uuid4()]
    now = datetime.now(timezone.utc)
    with postgres_session_factory() as db:
        db.add_all(
            [
                StorageCleanupTask(
                    id=task_id,
                    workspace_id=workspace_id,
                    document_id=uuid4(),
                    storage_backend="local",
                    storage_key=f"{uuid4().hex}.txt",
                    status="pending",
                    attempt_count=0,
                    next_attempt_at=now,
                )
                for task_id in task_ids
            ]
        )
        db.commit()

    barrier = Barrier(2)

    def claim_one():
        barrier.wait()
        return claim_cleanup_task(
            postgres_session_factory,
            workspace_id=workspace_id,
            now=now,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(lambda _index: claim_one(), range(2)))
        assert all(claim is not None for claim in claims)
        assert {claim.task_id for claim in claims} == set(task_ids)
        assert len({claim.lease_token for claim in claims}) == 2
    finally:
        with postgres_session_factory() as db:
            db.execute(
                delete(StorageCleanupTask).where(StorageCleanupTask.id.in_(task_ids))
            )
            db.commit()


def test_postgresql_local_to_r2_migration_dry_run_is_read_only(
    postgres_client, postgres_session_factory, tmp_path
):
    with postgres_session_factory() as db:
        db.execute(delete(KnowledgeChunk))
        db.execute(delete(KnowledgeDocument))
        db.commit()
    _token, user_id, workspace_id = _register(postgres_client, "storage-migration")
    storage_root = tmp_path / "source"
    storage_root.mkdir()
    settings = Settings(_env_file=None, knowledge_storage_path=storage_root)
    stored = LocalFilesystemDocumentStorage().put(
        BytesIO(b"synthetic migration dry-run content"),
        "dry-run.txt",
        settings,
        UUID(workspace_id),
    )
    with postgres_session_factory() as db:
        document = KnowledgeDocument(
            workspace_id=UUID(workspace_id),
            uploaded_by_id=UUID(user_id),
            filename=stored.filename,
            storage_key=stored.storage_key,
            media_type=stored.media_type,
            size_bytes=stored.size_bytes,
            embedding_provider="ollama",
            embedding_model="nomic-embed-text",
            embedding_dimensions=768,
            status="ready",
        )
        db.add(document)
        db.commit()
        document_id = document.id

    class NoR2Calls:
        def __getattr__(self, _name):
            raise AssertionError("Dry-run must not contact the destination.")

    report_path = tmp_path / "migration-report.json"
    with postgres_session_factory() as db:
        report = transfer_local_documents_to_r2(
            db,
            settings,
            report_path,
            dry_run=True,
            r2_storage=NoR2Calls(),
        )
    assert report["summary"]["discovered"] == 1
    assert report["summary"]["planned"] == 1
    with postgres_session_factory() as db:
        document_after = db.get(KnowledgeDocument, document_id)
        assert document_after is not None
        assert document_after.storage_key == stored.storage_key
        assert document_after.embedding_dimensions == 768
        assert document_after.status == "ready"
    with LocalFilesystemDocumentStorage().open(
        settings, UUID(workspace_id), stored.storage_key
    ) as source:
        assert source.read() == b"synthetic migration dry-run content"


def test_postgresql_registration_login_and_workspace_membership(
    postgres_client, postgres_session_factory
):
    email_suffix = uuid4().hex
    owner_email = f"owner-{email_suffix}@example.com"
    owner_response = postgres_client.post(
        "/api/v1/auth/register",
        json={
            "email": owner_email,
            "password": TEST_PASSWORD,
            "workspace_name": "PostgreSQL workspace",
        },
    )
    assert owner_response.status_code == 201
    owner_payload = owner_response.json()
    owner_id = owner_payload["user"]["id"]
    workspace_id = owner_payload["workspaces"][0]["id"]

    with postgres_session_factory() as db:
        owner = db.get(User, UUID(owner_id))
        workspace = db.get(Workspace, UUID(workspace_id))
        assert owner is not None
        assert owner.password_hash != TEST_PASSWORD
        assert owner.password_hash.startswith("$argon2")
        assert workspace is not None
        assert db.scalar(
            select(WorkspaceMembership.role).where(
                WorkspaceMembership.user_id == owner.id,
                WorkspaceMembership.workspace_id == workspace.id,
            )
        ) == "owner"

    login = postgres_client.post(
        "/api/v1/auth/login",
        json={"email": owner_email, "password": TEST_PASSWORD},
    )
    assert login.status_code == 200
    owner_token = login.json()["access_token"]

    member_email = f"member-{email_suffix}@example.com"
    member_response = postgres_client.post(
        "/api/v1/auth/register",
        json={
            "email": member_email,
            "password": TEST_PASSWORD,
            "workspace_name": "Member workspace",
        },
    )
    assert member_response.status_code == 201
    added = postgres_client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers=_auth(owner_token),
        json={"email": member_email},
    )
    assert added.status_code == 201
    assert added.json()["role"] == "member"

    members = postgres_client.get(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers=_auth(owner_token),
    )
    assert members.status_code == 200
    assert {member["email"] for member in members.json()} == {
        owner_email,
        member_email,
    }


def test_postgresql_conversation_chat_and_message_persistence(
    postgres_client, postgres_session_factory, monkeypatch
):
    token, _user_id, workspace_id = _register(postgres_client, "chat")

    class TestProvider:
        async def generate(self, message, history=None, rag_context=None):
            return f"PostgreSQL answer: {message}"

    from app.api.routes import chat as chat_routes

    monkeypatch.setattr(
        chat_routes, "create_ai_provider", lambda _settings: TestProvider()
    )
    response = postgres_client.post(
        "/api/v1/chat",
        headers=_auth(token),
        json={"message": "Persist this", "workspace_id": workspace_id},
    )
    assert response.status_code == 200
    conversation_id = response.json()["conversation_id"]
    assert response.json()["reply"] == "PostgreSQL answer: Persist this"

    messages = postgres_client.get(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=_auth(token),
    )
    assert messages.status_code == 200
    assert [item["role"] for item in messages.json()] == ["user", "assistant"]
    assert messages.json()[1]["content"] == "PostgreSQL answer: Persist this"

    with postgres_session_factory() as db:
        conversation_uuid = UUID(conversation_id)
        conversation = db.get(Conversation, conversation_uuid)
        stored_messages = list(
            db.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_uuid)
                .order_by(Message.sequence)
            )
        )
        assert conversation is not None
        assert conversation.last_message_sequence == 2
        assert [message.sequence for message in stored_messages] == [1, 2]

    listed = postgres_client.get(
        f"/api/v1/conversations?workspace_id={workspace_id}",
        headers=_auth(token),
    )
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == conversation_id


def test_postgresql_workspace_isolation_for_conversations_and_documents(
    postgres_client, postgres_session_factory
):
    owner_token, owner_id, workspace_id = _register(postgres_client, "tenant-owner")
    outsider_token, _outsider_id, _outsider_workspace = _register(
        postgres_client, "tenant-outsider"
    )
    conversation_response = postgres_client.post(
        "/api/v1/conversations",
        headers=_auth(owner_token),
        json={"workspace_id": workspace_id, "title": "Private"},
    )
    assert conversation_response.status_code == 201
    conversation_id = conversation_response.json()["id"]

    with postgres_session_factory() as db:
        document = KnowledgeDocument(
            workspace_id=UUID(workspace_id),
            uploaded_by_id=UUID(owner_id),
            filename="private.txt",
            storage_key=f"{uuid4().hex}.txt",
            media_type="text/plain",
            size_bytes=12,
            status="ready",
        )
        db.add(document)
        db.commit()
        document_id = document.id

    denied_conversation = postgres_client.get(
        f"/api/v1/conversations/{conversation_id}",
        headers=_auth(outsider_token),
    )
    denied_messages = postgres_client.get(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=_auth(outsider_token),
    )
    denied_documents = postgres_client.get(
        f"/api/v1/workspaces/{workspace_id}/documents",
        headers=_auth(outsider_token),
    )
    assert denied_conversation.status_code == 404
    assert denied_messages.status_code == 404
    assert denied_documents.status_code == 404

    with postgres_session_factory() as db:
        assert db.get(KnowledgeDocument, document_id) is not None
        assert db.scalar(
            select(func.count()).select_from(Conversation).where(
                Conversation.id == UUID(conversation_id)
            )
        ) == 1


def test_postgresql_document_metadata_and_rag_retrieval_are_workspace_scoped(
    postgres_client, postgres_session_factory
):
    token, owner_id, workspace_id = _register(postgres_client, "rag-owner")
    _other_token, _other_owner_id, other_workspace_id = _register(
        postgres_client, "rag-other"
    )

    with postgres_session_factory() as db:
        document = KnowledgeDocument(
            workspace_id=UUID(workspace_id),
            uploaded_by_id=UUID(owner_id),
            filename="benefits-guide.txt",
            storage_key=f"{uuid4().hex}.txt",
            media_type="text/plain",
            size_bytes=52,
            status="ready",
        )
        db.add(document)
        db.flush()
        chunk = KnowledgeChunk(
            document_id=document.id,
            workspace_id=document.workspace_id,
            sequence=0,
            content="The annual learning allowance is 1200 credits.",
            source_refs=["Page 4"],
            embedding=[1.0, 0.0, 0.0],
        )
        db.add(chunk)
        db.commit()
        document_id = document.id
        chunk_id = chunk.id

    with postgres_session_factory() as db:
        stored_document = db.get(KnowledgeDocument, document_id)
        stored_chunk = db.get(KnowledgeChunk, chunk_id)
        assert stored_document is not None
        assert stored_document.filename == "benefits-guide.txt"
        assert stored_document.status == "ready"
        assert stored_chunk is not None
        assert stored_chunk.source_refs == ["Page 4"]
        assert stored_chunk.embedding == [1.0, 0.0, 0.0]
        matches = retrieve_chunks(
            db,
            UUID(workspace_id),
            [1.0, 0.0, 0.0],
            limit=3,
            min_similarity=0.5,
        )
        cross_tenant_matches = retrieve_chunks(
            db,
            UUID(other_workspace_id),
            [1.0, 0.0, 0.0],
            limit=3,
            min_similarity=0.5,
        )
        assert len(matches) == 1
        assert matches[0].chunk_id == chunk_id
        assert matches[0].document_name == "benefits-guide.txt"
        assert matches[0].source_refs == ["Page 4"]
        assert cross_tenant_matches == []

    listed = postgres_client.get(
        f"/api/v1/workspaces/{workspace_id}/documents",
        headers=_auth(token),
    )
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == str(document_id)


def test_postgresql_constraints_and_transaction_rollback(
    postgres_session_factory,
):
    with postgres_session_factory() as db:
        user = User(
            email=f"constraint-{uuid4().hex}@example.com",
            password_hash="test-hash",
            is_active=True,
        )
        workspace = Workspace(name="Constraint workspace")
        db.add_all([user, workspace])
        db.flush()
        membership = WorkspaceMembership(
            workspace_id=workspace.id,
            user_id=user.id,
            role="owner",
        )
        db.add(membership)
        db.commit()
        user_id, workspace_id = user.id, workspace.id

        db.add(
            WorkspaceMembership(
                workspace_id=workspace_id,
                user_id=user_id,
                role="member",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        assert db.scalar(
            select(func.count()).select_from(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id == user_id,
            )
        ) == 1

        invalid_document_id = uuid4()
        invalid_document = KnowledgeDocument(
            id=invalid_document_id,
            workspace_id=workspace_id,
            uploaded_by_id=user_id,
            filename="invalid.txt",
            storage_key=f"{uuid4().hex}.txt",
            media_type="text/plain",
            size_bytes=1,
            status="invalid",
        )
        db.add(invalid_document)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        assert db.get(KnowledgeDocument, invalid_document_id) is None

    with postgres_session_factory() as db:
        rolled_back_workspace = Workspace(name="Must roll back")
        db.add(rolled_back_workspace)
        db.flush()
        rolled_back_id = rolled_back_workspace.id
        db.rollback()

    with postgres_session_factory() as db:
        assert db.get(Workspace, rolled_back_id) is None

    engine = postgres_session_factory.kw["bind"]
    workspace_indexes = {
        item["name"]
        for item in inspect(engine).get_indexes("workspace_memberships")
    }
    assert "ix_workspace_memberships_workspace_id" in workspace_indexes
    conversation_indexes = {
        item["name"] for item in inspect(engine).get_indexes("conversations")
    }
    assert "ix_conversations_workspace_id" in conversation_indexes
