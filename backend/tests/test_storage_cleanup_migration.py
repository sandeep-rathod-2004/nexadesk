from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.knowledge import KnowledgeDocument
from app.models.refresh_token import RefreshToken
from app.models.storage_cleanup import StorageCleanupReplayAudit
from app.models.user import User
from app.models.workspace import Workspace


_UNFINISHED_STATUSES = ("pending", "in_progress", "permanently_failed")
_MIGRATION_BASE = "0005_embedding_identity"
_OUTBOX_REVISION = "0006_storage_cleanup_outbox"
_BACKEND_REVISION = "0007_document_storage_backend"
_AUDIT_REVISION = "0008_cleanup_replay_audit"
_FULL_NAME_REVISION = "0009_user_full_name"
_HEAD_REVISION = "0011_chat_types"


def _migration_config(database_url: str, monkeypatch) -> Config:
    backend_dir = Path(__file__).resolve().parents[1]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    config.config_file_name = None
    settings = Settings(_env_file=None, database_url=database_url)

    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    return config


def test_migration_revision_ids_fit_postgresql_version_column_and_graph_is_valid(
    tmp_path, monkeypatch
):
    database_url = f"sqlite:///{(tmp_path / 'revision-graph.db').as_posix()}"
    config = _migration_config(database_url, monkeypatch)
    script = ScriptDirectory.from_config(config)
    revisions = {revision.revision: revision for revision in script.walk_revisions()}

    assert revisions
    assert all(len(revision_id) <= 32 for revision_id in revisions)
    assert script.get_heads() == [_HEAD_REVISION]
    for revision in revisions.values():
        parents = revision.down_revision
        if parents is None:
            continue
        parent_ids = (parents,) if isinstance(parents, str) else parents
        assert all(parent_id in revisions for parent_id in parent_ids)


def test_refresh_token_migration_creates_rotation_indexes_and_guards_downgrade(
    migrated_head,
):
    config, engine = migrated_head
    inspector = inspect(engine)
    assert "refresh_tokens" in inspector.get_table_names()
    assert {index["name"] for index in inspector.get_indexes("refresh_tokens")} == {
        "ix_refresh_tokens_user_id",
        "ix_refresh_tokens_family_id",
        "ix_refresh_tokens_expires_at",
    }

    with Session(engine) as db:
        user = User(email=f"{uuid4().hex}@example.test", password_hash="synthetic")
        db.add(user)
        db.flush()
        db.add(
            RefreshToken(
                user_id=user.id,
                family_id=uuid4(),
                token_hash="a" * 64,
                expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            )
        )
        db.commit()

    with pytest.raises(RuntimeError, match="refresh-token records exist"):
        command.downgrade(config, "0009_user_full_name")

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0010_refresh_tokens"
        )
        assert connection.scalar(text("SELECT COUNT(*) FROM refresh_tokens")) == 1


def _insert_cleanup_task(engine, status: str) -> str:
    task_id = uuid4().hex
    values = {
        "id": task_id,
        "workspace_id": uuid4().hex,
        "document_id": uuid4().hex,
        "storage_backend": "local",
        "storage_key": f"{uuid4().hex}.txt",
        "status": status,
        "attempt_count": 1,
        "next_attempt_at": "2026-01-01 00:00:00",
        "lease_token": uuid4().hex if status == "in_progress" else None,
        "lease_expires_at": (
            "2026-01-01 00:02:00" if status == "in_progress" else None
        ),
        "last_error_code": (
            "storage_unavailable" if status == "permanently_failed" else None
        ),
        "created_at": "2026-01-01 00:00:00",
        "updated_at": "2026-01-01 00:00:00",
        "completed_at": None,
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO storage_cleanup_tasks "
                "(id, workspace_id, document_id, storage_backend, storage_key, "
                "status, attempt_count, next_attempt_at, lease_token, "
                "lease_expires_at, last_error_code, created_at, updated_at, "
                "completed_at) VALUES "
                "(:id, :workspace_id, :document_id, :storage_backend, :storage_key, "
                ":status, :attempt_count, :next_attempt_at, :lease_token, "
                ":lease_expires_at, :last_error_code, :created_at, :updated_at, "
                ":completed_at)"
            ),
            values,
        )
    return task_id


@pytest.fixture
def migrated_outbox(tmp_path, monkeypatch):
    database_path = tmp_path / "outbox-migration.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = _migration_config(database_url, monkeypatch)
    command.upgrade(config, _OUTBOX_REVISION)
    engine = create_engine(database_url)
    try:
        yield config, engine
    finally:
        engine.dispose()


@pytest.fixture
def migrated_head(tmp_path, monkeypatch):
    database_path = tmp_path / "storage-cleanup-head.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = _migration_config(database_url, monkeypatch)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        yield config, engine
    finally:
        engine.dispose()


def _insert_backend_mapping_and_replay_audit(engine, task_id: str) -> tuple[str, str]:
    with Session(engine) as db:
        user = User(email=f"{uuid4().hex}@example.test", password_hash="synthetic")
        workspace = Workspace(name="synthetic migration test")
        db.add_all([user, workspace])
        db.flush()
        document = KnowledgeDocument(
            workspace_id=workspace.id,
            uploaded_by_id=user.id,
            filename="synthetic.txt",
            storage_key=f"{uuid4().hex}.txt",
            storage_backend="local",
            media_type="text/plain",
            size_bytes=1,
            status="failed",
        )
        db.add(document)
        db.flush()
        audit = StorageCleanupReplayAudit(
            task_id=UUID(hex=task_id),
            workspace_id=workspace.id,
            document_id=document.id,
            operator_user_id=user.id,
            storage_backend="local",
            storage_key=f"{uuid4().hex}.txt",
            previous_attempt_count=5,
            previous_error_code="storage_unavailable",
            replayed_at=datetime.now(timezone.utc),
        )
        db.add(audit)
        db.commit()
        return document.storage_key, audit.storage_key


@pytest.mark.parametrize("status", _UNFINISHED_STATUSES)
def test_downgrade_refuses_each_unfinished_status_and_preserves_schema_and_task(
    migrated_outbox, status
):
    config, engine = migrated_outbox
    task_id = _insert_cleanup_task(engine, status)

    with pytest.raises(RuntimeError, match="Cannot downgrade storage cleanup outbox"):
        command.downgrade(config, _MIGRATION_BASE)

    assert "storage_cleanup_tasks" in inspect(engine).get_table_names()
    assert "ix_storage_cleanup_due" in {
        index["name"] for index in inspect(engine).get_indexes("storage_cleanup_tasks")
    }
    with engine.connect() as connection:
        task = connection.execute(
            text(
                "SELECT id, status, storage_key, storage_backend "
                "FROM storage_cleanup_tasks WHERE id = :task_id"
            ),
            {"task_id": task_id},
        ).one()
        assert task.status == status
        assert task.storage_key.endswith(".txt")
        assert task.storage_backend == "local"
        assert connection.scalar(text("SELECT COUNT(*) FROM storage_cleanup_tasks")) == 1
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            _OUTBOX_REVISION
        )


def test_downgrade_allows_only_completed_tasks_and_drops_the_outbox(
    migrated_outbox,
):
    config, engine = migrated_outbox
    _insert_cleanup_task(engine, "completed")

    command.downgrade(config, _MIGRATION_BASE)

    assert "storage_cleanup_tasks" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            _MIGRATION_BASE
        )


def test_outbox_upgrade_downgrade_reupgrade_on_disposable_database(migrated_outbox):
    config, engine = migrated_outbox

    command.downgrade(config, _MIGRATION_BASE)
    assert "storage_cleanup_tasks" not in inspect(engine).get_table_names()

    command.upgrade(config, _OUTBOX_REVISION)

    assert "storage_cleanup_tasks" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            _OUTBOX_REVISION
        )


def test_full_chain_downgrade_and_reupgrade_on_disposable_database(migrated_head):
    config, engine = migrated_head

    command.downgrade(config, _MIGRATION_BASE)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            _MIGRATION_BASE
        )
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            _HEAD_REVISION
        )
        assert "storage_backend" in {
            column["name"]
            for column in inspect(engine).get_columns("knowledge_documents")
        }
        assert "is_admin" in {
            column["name"] for column in inspect(engine).get_columns("users")
        }
        assert "storage_cleanup_replay_audits" in inspect(engine).get_table_names()


@pytest.mark.parametrize("status", _UNFINISHED_STATUSES)
def test_full_downgrade_from_head_preserves_data_after_partial_downgrade(
    migrated_head, status
):
    config, engine = migrated_head
    task_id = _insert_cleanup_task(engine, status)
    document_key, audit_key = _insert_backend_mapping_and_replay_audit(engine, task_id)

    before_tables = set(inspect(engine).get_table_names())
    before_columns = {
        table: {column["name"] for column in inspect(engine).get_columns(table)}
        for table in before_tables
    }

    with pytest.raises(RuntimeError, match="unfinished cleanup tasks exist"):
        command.downgrade(config, _MIGRATION_BASE)

    # SQLite completes the empty 0011 and 0010 downgrades before 0009's data guard refuses.
    expected_tables = before_tables - {
        "conversation_documents",
        "refresh_tokens",
    }
    assert set(inspect(engine).get_table_names()) == expected_tables
    expected_columns = {
        table: columns
        for table, columns in before_columns.items()
        if table in expected_tables
    }
    expected_columns["conversations"].remove("chat_type")
    assert {
        table: {column["name"] for column in inspect(engine).get_columns(table)}
        for table in expected_tables
    } == expected_columns
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            _FULL_NAME_REVISION
        )
        task = connection.execute(
            text(
                "SELECT status, storage_backend, storage_key "
                "FROM storage_cleanup_tasks WHERE id = :task_id"
            ),
            {"task_id": task_id},
        ).one()
        assert task.status == status
        assert task.storage_backend == "local"
        assert task.storage_key.endswith(".txt")
        assert connection.scalar(
            text(
                "SELECT COUNT(*) FROM knowledge_documents "
                "WHERE storage_key = :storage_key AND storage_backend = 'local'"
            ),
            {"storage_key": document_key},
        ) == 1
        assert connection.scalar(
            text(
                "SELECT COUNT(*) FROM storage_cleanup_replay_audits "
                "WHERE storage_key = :storage_key"
            ),
            {"storage_key": audit_key},
        ) == 1


def test_downgrade_0008_preserves_replay_audit_history(migrated_head):
    config, engine = migrated_head
    command.downgrade(config, _AUDIT_REVISION)
    _document_key, audit_key = _insert_backend_mapping_and_replay_audit_at_0008(engine)
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM knowledge_documents WHERE storage_key = :storage_key"),
            {"storage_key": _document_key},
        )

    with pytest.raises(RuntimeError, match="replay audit records exist"):
        command.downgrade(config, _BACKEND_REVISION)

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            _AUDIT_REVISION
        )
        assert connection.scalar(
            text("SELECT COUNT(*) FROM storage_cleanup_replay_audits")
        ) == 1


def test_downgrade_0008_preserves_backend_mappings_before_schema_change(migrated_head):
    config, engine = migrated_head
    command.downgrade(config, _AUDIT_REVISION)
    document_key, _audit_key = _insert_backend_mapping_and_replay_audit_at_0008(engine)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM storage_cleanup_replay_audits"))

    with pytest.raises(RuntimeError, match="document backend mappings exist"):
        command.downgrade(config, _BACKEND_REVISION)

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            _AUDIT_REVISION
        )
        assert connection.scalar(
            text(
                "SELECT COUNT(*) FROM knowledge_documents "
                "WHERE storage_key = :storage_key AND storage_backend = 'local'"
            ),
            {"storage_key": document_key},
        ) == 1


def test_downgrade_0008_preserves_admin_assignments(migrated_head):
    config, engine = migrated_head
    command.downgrade(config, _AUDIT_REVISION)
    user_id = uuid4().hex
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, email, password_hash, is_active, is_admin) "
                "VALUES (:id, :email, :password_hash, :is_active, :is_admin)"
            ),
            {
                "id": user_id,
                "email": f"{user_id}@example.test",
                "password_hash": "synthetic",
                "is_active": True,
                "is_admin": True,
            },
        )

    with pytest.raises(RuntimeError, match="admin user records exist"):
        command.downgrade(config, _BACKEND_REVISION)

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            _AUDIT_REVISION
        )
        assert connection.scalar(
            text("SELECT is_admin FROM users WHERE id = :user_id"),
            {"user_id": user_id},
        )
        assert "is_admin" in {
            column["name"] for column in inspect(engine).get_columns("users")
        }


def test_downgrade_0009_preserves_populated_full_names(migrated_head):
    config, engine = migrated_head
    command.downgrade(config, _FULL_NAME_REVISION)
    with Session(engine) as db:
        user = User(
            email=f"{uuid4().hex}@example.test",
            password_hash="synthetic",
            full_name="Ada Lovelace",
        )
        db.add(user)
        db.commit()
        user_id = user.id

    with pytest.raises(RuntimeError, match="user full-name data exists"):
        command.downgrade(config, "0008_cleanup_replay_audit")

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            _FULL_NAME_REVISION
        )
        assert connection.scalar(
            text("SELECT full_name FROM users WHERE id = :user_id"),
            {"user_id": user_id.hex},
        ) == "Ada Lovelace"


def _insert_backend_mapping_and_replay_audit_at_0008(engine) -> tuple[str, str]:
    user_id = uuid4().hex
    workspace_id = uuid4().hex
    document_id = uuid4().hex
    document_key = f"{uuid4().hex}.txt"
    audit_key = f"{uuid4().hex}.txt"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, email, password_hash, is_active, is_admin) "
                "VALUES (:id, :email, :password_hash, :is_active, :is_admin)"
            ),
            {
                "id": user_id,
                "email": f"{user_id}@example.test",
                "password_hash": "synthetic",
                "is_active": True,
                "is_admin": False,
            },
        )
        connection.execute(
            text("INSERT INTO workspaces (id, name) VALUES (:id, :name)"),
            {"id": workspace_id, "name": "synthetic migration test"},
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_documents "
                "(id, workspace_id, uploaded_by_id, filename, storage_key, "
                "storage_backend, media_type, size_bytes, status) "
                "VALUES (:id, :workspace_id, :uploaded_by_id, :filename, "
                ":storage_key, :storage_backend, :media_type, :size_bytes, :status)"
            ),
            {
                "id": document_id,
                "workspace_id": workspace_id,
                "uploaded_by_id": user_id,
                "filename": "synthetic.txt",
                "storage_key": document_key,
                "storage_backend": "local",
                "media_type": "text/plain",
                "size_bytes": 1,
                "status": "failed",
            },
        )
        connection.execute(
            text(
                "INSERT INTO storage_cleanup_replay_audits "
                "(id, task_id, workspace_id, document_id, operator_user_id, "
                "storage_backend, storage_key, previous_attempt_count, "
                "previous_error_code) VALUES "
                "(:id, :task_id, :workspace_id, :document_id, :operator_user_id, "
                ":storage_backend, :storage_key, :previous_attempt_count, "
                ":previous_error_code)"
            ),
            {
                "id": uuid4().hex,
                "task_id": uuid4().hex,
                "workspace_id": workspace_id,
                "document_id": document_id,
                "operator_user_id": user_id,
                "storage_backend": "local",
                "storage_key": audit_key,
                "previous_attempt_count": 5,
                "previous_error_code": "storage_unavailable",
            },
        )
    return document_key, audit_key


def test_downgrade_0007_preserves_document_backend_mappings(migrated_head):
    config, engine = migrated_head
    with Session(engine) as db:
        user = User(email=f"{uuid4().hex}@example.test", password_hash="synthetic")
        workspace = Workspace(name="synthetic migration test")
        db.add_all([user, workspace])
        db.commit()
        user_id, workspace_id = user.id, workspace.id
    command.downgrade(config, _BACKEND_REVISION)

    document_key = f"{uuid4().hex}.txt"
    with Session(engine) as db:
        db.add(
            KnowledgeDocument(
                workspace_id=workspace_id,
                uploaded_by_id=user_id,
                filename="synthetic.txt",
                storage_key=document_key,
                storage_backend="local",
                media_type="text/plain",
                size_bytes=1,
                status="failed",
            )
        )
        db.commit()

    with pytest.raises(RuntimeError, match="document backend mappings exist"):
        command.downgrade(config, _OUTBOX_REVISION)

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            _BACKEND_REVISION
        )
        assert connection.scalar(
            text(
                "SELECT COUNT(*) FROM knowledge_documents "
                "WHERE storage_key = :storage_key AND storage_backend = 'local'"
            ),
            {"storage_key": document_key},
        ) == 1


def test_downgrade_0007_refuses_unfinished_cleanup_tasks(migrated_head):
    config, engine = migrated_head
    command.downgrade(config, _BACKEND_REVISION)
    task_id = _insert_cleanup_task(engine, "pending")

    with pytest.raises(RuntimeError, match="unfinished cleanup tasks exist"):
        command.downgrade(config, _OUTBOX_REVISION)

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            _BACKEND_REVISION
        )
        assert connection.scalar(
            text("SELECT status FROM storage_cleanup_tasks WHERE id = :task_id"),
            {"task_id": task_id},
        ) == "pending"
        assert "storage_backend" in {
            column["name"]
            for column in inspect(engine).get_columns("knowledge_documents")
        }
