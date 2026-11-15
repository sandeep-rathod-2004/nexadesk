from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.models.storage_cleanup import (
    StorageCleanupReplayAudit,
    StorageCleanupTask,
)
from app.models.user import User
from app.models.workspace import WorkspaceMembership
from app.services.knowledge_storage import DocumentStorageError
from app.workers.storage_cleanup import (
    CleanupClaim,
    _backoff_seconds,
    _finish_success,
    claim_cleanup_task,
    process_next_cleanup_task,
)


PASSWORD = "correct horse battery staple"


def register(client, email: str) -> tuple[str, UUID, UUID]:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "workspace_name": f"{email} workspace",
        },
    )
    assert response.status_code == 201
    data = response.json()
    authenticated = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": PASSWORD},
    )
    assert authenticated.status_code == 200
    return (
        authenticated.json()["access_token"],
        UUID(data["user"]["id"]),
        UUID(data["workspaces"][0]["id"]),
    )


def create_document(
    session_factory,
    *,
    workspace_id: UUID,
    uploader_id: UUID,
    storage_key: str | None = None,
    storage_backend: str | None = "local",
) -> tuple[UUID, str]:
    key = storage_key or f"{uuid4().hex}.txt"
    document = KnowledgeDocument(
        workspace_id=workspace_id,
        uploaded_by_id=uploader_id,
        filename="synthetic.txt",
        storage_key=key,
        storage_backend=storage_backend,
        media_type="text/plain",
        size_bytes=16,
        status="ready",
    )
    with session_factory() as db:
        db.add(document)
        db.flush()
        db.add(
            KnowledgeChunk(
                document_id=document.id,
                workspace_id=workspace_id,
                sequence=0,
                content="synthetic content",
                source_refs=["Text file"],
                embedding=[1.0, 0.0],
            )
        )
        document_id = document.id
        db.commit()
    return document_id, key


def create_task(
    session_factory,
    *,
    workspace_id: UUID | None = None,
    document_id: UUID | None = None,
    storage_key: str | None = None,
    backend: str = "local",
    status: str = "pending",
    attempt_count: int = 0,
    next_attempt_at: datetime | None = None,
    lease_token: UUID | None = None,
    lease_expires_at: datetime | None = None,
) -> UUID:
    task = StorageCleanupTask(
        workspace_id=workspace_id or uuid4(),
        document_id=document_id or uuid4(),
        storage_backend=backend,
        storage_key=storage_key or f"{uuid4().hex}.txt",
        status=status,
        attempt_count=attempt_count,
        next_attempt_at=next_attempt_at or datetime.now(timezone.utc),
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
    )
    with session_factory() as db:
        db.add(task)
        db.commit()
        return task.id


class FakeStorage:
    def __init__(self):
        self.objects: set[tuple[UUID, str]] = set()
        self.delete_calls: list[tuple[UUID, str]] = []
        self.fail_delete = False

    def exists(self, _settings, workspace_id: UUID, storage_key: str) -> bool:
        return (workspace_id, storage_key) in self.objects

    def delete(self, _settings, workspace_id: UUID, storage_key: str) -> None:
        self.delete_calls.append((workspace_id, storage_key))
        if self.fail_delete:
            raise DocumentStorageError(
                "provider-secret https://internal-provider.invalid"
            )
        self.objects.discard((workspace_id, storage_key))


def request_delete(client, token: str, workspace_id: UUID, document_id: UUID):
    return client.delete(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}",
        headers={"Authorization": f"Bearer {token}"},
    )


def grant_admin(session_factory, user_id: UUID) -> None:
    with session_factory() as db:
        user = db.get(User, user_id)
        assert user is not None
        user.is_admin = True
        db.commit()


def replay_task(client, token: str, task_id: UUID):
    return client.post(
        f"/api/v1/admin/storage-cleanup-tasks/{task_id}/replay",
        headers={"Authorization": f"Bearer {token}"},
    )


def test_uploader_deletion_atomically_removes_document_chunks_and_enqueues_task(
    client, test_session_factory, monkeypatch
):
    token, user_id, workspace_id = register(client, f"uploader-{uuid4().hex}@example.com")
    document_id, storage_key = create_document(
        test_session_factory, workspace_id=workspace_id, uploader_id=user_id
    )
    from app.api.routes import knowledge as knowledge_routes

    settings = Settings(_env_file=None)
    monkeypatch.setattr(knowledge_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(
        knowledge_routes,
        "get_document_storage",
        lambda _settings: (_ for _ in ()).throw(AssertionError("request must not delete storage")),
    )

    response = request_delete(client, token, workspace_id, document_id)

    assert response.status_code == 202
    assert response.json() == {"status": "deleted", "storage_cleanup": "pending"}
    with test_session_factory() as db:
        assert db.get(KnowledgeDocument, document_id) is None
        assert list(
            db.scalars(
                select(KnowledgeChunk).where(
                    KnowledgeChunk.document_id == document_id
                )
            )
        ) == []
        tasks = list(
            db.scalars(
                select(StorageCleanupTask).where(
                    StorageCleanupTask.document_id == document_id
                )
            )
        )
        assert len(tasks) == 1
        assert tasks[0].workspace_id == workspace_id
        assert tasks[0].storage_key == storage_key
        assert tasks[0].storage_backend == "local"
        assert tasks[0].status == "pending"


def test_workspace_owner_can_delete_another_members_document(
    client, test_session_factory, monkeypatch
):
    owner_token, _owner_id, workspace_id = register(
        client, f"owner-{uuid4().hex}@example.com"
    )
    _member_token, uploader_id, _ = register(
        client, f"uploader-{uuid4().hex}@example.com"
    )
    with test_session_factory() as db:
        db.add(
            WorkspaceMembership(
                workspace_id=workspace_id, user_id=uploader_id, role="member"
            )
        )
        db.commit()
    document_id, _ = create_document(
        test_session_factory, workspace_id=workspace_id, uploader_id=uploader_id
    )
    from app.api.routes import knowledge as knowledge_routes

    monkeypatch.setattr(
        knowledge_routes, "get_settings", lambda: Settings(_env_file=None)
    )

    response = request_delete(client, owner_token, workspace_id, document_id)

    assert response.status_code == 202
    with test_session_factory() as db:
        assert db.scalar(
            select(StorageCleanupTask.id).where(
                StorageCleanupTask.document_id == document_id
            )
        ) is not None


def test_member_cannot_delete_another_users_document(
    client, test_session_factory, monkeypatch
):
    _owner_token, _owner_id, workspace_id = register(
        client, f"owner-{uuid4().hex}@example.com"
    )
    uploader_token, uploader_id, _ = register(
        client, f"uploader-{uuid4().hex}@example.com"
    )
    _member_token, member_id, _ = register(
        client, f"member-{uuid4().hex}@example.com"
    )
    with test_session_factory() as db:
        db.add(
            WorkspaceMembership(
                workspace_id=workspace_id, user_id=uploader_id, role="member"
            )
        )
        db.add(
            WorkspaceMembership(
                workspace_id=workspace_id, user_id=member_id, role="member"
            )
        )
        db.commit()
    document_id, _ = create_document(
        test_session_factory, workspace_id=workspace_id, uploader_id=uploader_id
    )
    from app.api.routes import knowledge as knowledge_routes

    monkeypatch.setattr(
        knowledge_routes, "get_settings", lambda: Settings(_env_file=None)
    )
    response = request_delete(client, _member_token, workspace_id, document_id)
    assert response.status_code == 403
    with test_session_factory() as db:
        assert db.get(KnowledgeDocument, document_id) is not None
        assert db.scalar(
            select(StorageCleanupTask.id).where(
                StorageCleanupTask.document_id == document_id
            )
        ) is None
    assert uploader_token


def test_deletion_transaction_rollback_preserves_document_chunks_and_omits_task(
    client, test_session_factory, monkeypatch
):
    token, user_id, workspace_id = register(client, f"rollback-{uuid4().hex}@example.com")
    document_id, _ = create_document(
        test_session_factory, workspace_id=workspace_id, uploader_id=user_id
    )
    from app.api.routes import knowledge as knowledge_routes

    monkeypatch.setattr(
        knowledge_routes, "get_settings", lambda: Settings(_env_file=None)
    )

    def fail_commit(session):
        if any(isinstance(item, StorageCleanupTask) for item in session.new):
            raise SQLAlchemyError("synthetic transaction failure")

    event.listen(Session, "before_commit", fail_commit)
    try:
        response = request_delete(client, token, workspace_id, document_id)
    finally:
        event.remove(Session, "before_commit", fail_commit)

    assert response.status_code == 500
    assert response.json()["detail"] == "The document could not be deleted."
    with test_session_factory() as db:
        assert db.get(KnowledgeDocument, document_id) is not None
        assert db.scalar(
            select(KnowledgeChunk.id).where(
                KnowledgeChunk.document_id == document_id
            )
        ) is not None
        assert db.scalar(
            select(StorageCleanupTask.id).where(
                StorageCleanupTask.document_id == document_id
            )
        ) is None


def test_completed_task_does_not_block_a_later_valid_cleanup(
    client, test_session_factory, monkeypatch
):
    token, user_id, workspace_id = register(client, f"duplicate-{uuid4().hex}@example.com")
    document_id, key = create_document(
        test_session_factory, workspace_id=workspace_id, uploader_id=user_id
    )
    with test_session_factory() as db:
        db.add(
            StorageCleanupTask(
                workspace_id=workspace_id,
                document_id=document_id,
                storage_backend="local",
                storage_key=key,
                status="completed",
                attempt_count=1,
                completed_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
    from app.api.routes import knowledge as knowledge_routes

    monkeypatch.setattr(
        knowledge_routes, "get_settings", lambda: Settings(_env_file=None)
    )

    response = request_delete(client, token, workspace_id, document_id)

    assert response.status_code == 202
    with test_session_factory() as db:
        assert list(
            db.scalars(
                select(StorageCleanupTask.status).where(
                    StorageCleanupTask.document_id == document_id
                ).order_by(StorageCleanupTask.created_at)
            )
        ) == ["completed", "pending"]


def test_existing_pending_task_is_reused_and_keeps_recorded_backend(
    client, test_session_factory, monkeypatch
):
    token, user_id, workspace_id = register(client, f"reuse-{uuid4().hex}@example.com")
    document_id, key = create_document(
        test_session_factory, workspace_id=workspace_id, uploader_id=user_id
    )
    task_id = create_task(
        test_session_factory,
        workspace_id=workspace_id,
        document_id=document_id,
        storage_key=key,
        backend="r2",
    )
    with test_session_factory() as db:
        db.get(KnowledgeDocument, document_id).storage_backend = "r2"
        db.commit()
    from app.api.routes import knowledge as knowledge_routes

    monkeypatch.setattr(
        knowledge_routes,
        "get_settings",
        lambda: Settings(_env_file=None, document_storage_backend="local"),
    )
    response = request_delete(client, token, workspace_id, document_id)
    assert response.status_code == 202
    with test_session_factory() as db:
        tasks = list(
            db.scalars(
                select(StorageCleanupTask).where(
                    StorageCleanupTask.document_id == document_id
                )
            )
        )
        assert len(tasks) == 1
        assert tasks[0].id == task_id
        assert tasks[0].storage_backend == "r2"


def test_delete_uses_document_backend_not_current_global_backend(
    client, test_session_factory, monkeypatch
):
    token, user_id, workspace_id = register(client, f"backend-{uuid4().hex}@example.com")
    document_id, _ = create_document(
        test_session_factory,
        workspace_id=workspace_id,
        uploader_id=user_id,
        storage_backend="r2",
    )
    from app.api.routes import knowledge as knowledge_routes

    monkeypatch.setattr(
        knowledge_routes,
        "get_settings",
        lambda: Settings(_env_file=None, document_storage_backend="local"),
    )
    response = request_delete(client, token, workspace_id, document_id)
    assert response.status_code == 202
    with test_session_factory() as db:
        task = db.scalar(
            select(StorageCleanupTask).where(
                StorageCleanupTask.document_id == document_id
            )
        )
        assert task is not None
        assert task.storage_backend == "r2"


def test_download_selects_document_backend_not_current_global_backend(
    client, test_session_factory, monkeypatch
):
    token, user_id, workspace_id = register(client, f"download-{uuid4().hex}@example.com")
    document_id, _ = create_document(
        test_session_factory,
        workspace_id=workspace_id,
        uploader_id=user_id,
        storage_backend="r2",
    )
    from app.api.routes import knowledge as knowledge_routes

    selected_backends: list[str] = []

    class MissingObjectStorage:
        def exists(self, _settings, _workspace_id, _storage_key):
            return False

    monkeypatch.setattr(
        knowledge_routes,
        "get_settings",
        lambda: Settings(_env_file=None, document_storage_backend="local"),
    )
    monkeypatch.setattr(
        knowledge_routes,
        "get_document_storage_for_backend",
        lambda _settings, backend: (
            selected_backends.append(backend) or MissingObjectStorage()
        ),
    )
    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}/download",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404
    assert selected_backends == ["r2"]


def test_delete_rejects_invalid_storage_key_without_removing_document(
    client, test_session_factory, monkeypatch
):
    token, user_id, workspace_id = register(client, f"bad-key-{uuid4().hex}@example.com")
    document_id, _ = create_document(
        test_session_factory,
        workspace_id=workspace_id,
        uploader_id=user_id,
        storage_key="../outside.txt",
    )
    from app.api.routes import knowledge as knowledge_routes

    monkeypatch.setattr(
        knowledge_routes, "get_settings", lambda: Settings(_env_file=None)
    )
    response = request_delete(client, token, workspace_id, document_id)
    assert response.status_code == 500
    with test_session_factory() as db:
        assert db.get(KnowledgeDocument, document_id) is not None
        assert db.scalar(
            select(StorageCleanupTask.id).where(
                StorageCleanupTask.document_id == document_id
            )
        ) is None


def test_legacy_document_without_verified_backend_is_not_deleted(
    client, test_session_factory, monkeypatch
):
    token, user_id, workspace_id = register(client, f"legacy-{uuid4().hex}@example.com")
    document_id, _ = create_document(
        test_session_factory,
        workspace_id=workspace_id,
        uploader_id=user_id,
        storage_backend=None,
    )
    from app.api.routes import knowledge as knowledge_routes

    monkeypatch.setattr(
        knowledge_routes,
        "get_settings",
        lambda: Settings(_env_file=None, document_storage_backend="r2"),
    )
    response = request_delete(client, token, workspace_id, document_id)
    assert response.status_code == 409
    with test_session_factory() as db:
        assert db.get(KnowledgeDocument, document_id) is not None
        assert db.scalar(
            select(StorageCleanupTask.id).where(
                StorageCleanupTask.document_id == document_id
            )
        ) is None


def test_worker_deletes_object_and_marks_task_completed(test_session_factory):
    workspace_id, key = uuid4(), f"{uuid4().hex}.txt"
    task_id = create_task(
        test_session_factory, workspace_id=workspace_id, storage_key=key
    )
    storage = FakeStorage()
    storage.objects.add((workspace_id, key))
    result = process_next_cleanup_task(
        test_session_factory,
        Settings(_env_file=None),
        storage_factory=lambda _settings, _backend: storage,
    )
    assert result == "completed"
    assert storage.delete_calls == [(workspace_id, key)]
    with test_session_factory() as db:
        task = db.get(StorageCleanupTask, task_id)
        assert task is not None
        assert task.status == "completed"
        assert task.completed_at is not None
        assert task.lease_token is None


def test_worker_treats_already_missing_object_as_success(test_session_factory):
    task_id = create_task(test_session_factory)
    storage = FakeStorage()
    assert process_next_cleanup_task(
        test_session_factory,
        Settings(_env_file=None),
        storage_factory=lambda _settings, _backend: storage,
    ) == "completed"
    assert storage.delete_calls == []
    with test_session_factory() as db:
        assert db.get(StorageCleanupTask, task_id).status == "completed"


def test_transient_storage_failure_schedules_retry_with_safe_error_code(
    test_session_factory, monkeypatch, caplog
):
    task_id = create_task(test_session_factory)
    storage = FakeStorage()
    storage.fail_delete = True
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    with test_session_factory() as db:
        task = db.get(StorageCleanupTask, task_id)
        task.next_attempt_at = now
        db.commit()
        workspace_id, key = task.workspace_id, task.storage_key
    storage.objects.add((workspace_id, key))
    monkeypatch.setattr("app.workers.storage_cleanup._backoff_seconds", lambda _n: 17)
    result = process_next_cleanup_task(
        test_session_factory,
        Settings(_env_file=None),
        now=now,
        storage_factory=lambda _settings, _backend: storage,
    )
    assert result == "retry_scheduled"
    with test_session_factory() as db:
        task = db.get(StorageCleanupTask, task_id)
        assert task.status == "pending"
        assert task.attempt_count == 1
        assert task.last_error_code == "storage_unavailable"
        assert task.next_attempt_at.replace(tzinfo=timezone.utc) == now + timedelta(seconds=17)
    assert "provider-secret" not in caplog.text
    assert "internal-provider.invalid" not in caplog.text


def test_worker_marks_task_permanently_failed_at_maximum_attempts(
    test_session_factory
):
    task_id = create_task(test_session_factory)
    storage = FakeStorage()
    storage.fail_delete = True
    with test_session_factory() as db:
        task = db.get(StorageCleanupTask, task_id)
        storage.objects.add((task.workspace_id, task.storage_key))
    assert process_next_cleanup_task(
        test_session_factory,
        Settings(_env_file=None),
        max_attempts=1,
        storage_factory=lambda _settings, _backend: storage,
    ) == "permanently_failed"
    with test_session_factory() as db:
        task = db.get(StorageCleanupTask, task_id)
        assert task.status == "permanently_failed"
        assert task.attempt_count == 1
        assert task.last_error_code == "storage_unavailable"
        assert task.lease_token is None


def test_retry_backoff_is_jittered_and_capped(monkeypatch):
    observed: list[tuple[float, float]] = []

    def deterministic_jitter(lower: float, upper: float) -> float:
        observed.append((lower, upper))
        return upper / 2

    monkeypatch.setattr("app.workers.storage_cleanup.random.uniform", deterministic_jitter)
    assert _backoff_seconds(1) == 2.5
    assert _backoff_seconds(100_000) == 1800
    assert observed == [(0, 5), (0, 3600)]


def test_expired_lease_is_reclaimed_with_new_token(test_session_factory):
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    old_token = uuid4()
    task_id = create_task(
        test_session_factory,
        status="in_progress",
        attempt_count=1,
        lease_token=old_token,
        lease_expires_at=now - timedelta(seconds=1),
    )
    claim = claim_cleanup_task(
        test_session_factory, now=now, max_attempts=3, lease_seconds=30
    )
    assert claim is not None
    assert claim.task_id == task_id
    assert claim.lease_token != old_token
    assert claim.attempt_count == 2
    with test_session_factory() as db:
        task = db.get(StorageCleanupTask, task_id)
        assert task.status == "in_progress"
        assert task.lease_token == claim.lease_token
        assert task.lease_expires_at.replace(tzinfo=timezone.utc) == now + timedelta(seconds=30)


def test_expired_lease_at_attempt_limit_is_preserved_as_permanent_failure(
    test_session_factory
):
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    task_id = create_task(
        test_session_factory,
        status="in_progress",
        attempt_count=2,
        lease_token=uuid4(),
        lease_expires_at=now - timedelta(seconds=1),
    )
    assert claim_cleanup_task(
        test_session_factory, now=now, max_attempts=2
    ) is None
    with test_session_factory() as db:
        task = db.get(StorageCleanupTask, task_id)
        assert task.status == "permanently_failed"
        assert task.last_error_code == "lease_expired"
        assert task.lease_token is None


def test_stale_lease_token_cannot_mark_task_completed(test_session_factory):
    task_id = create_task(test_session_factory)
    claim = claim_cleanup_task(test_session_factory)
    assert claim is not None and claim.task_id == task_id
    stale_claim = CleanupClaim(
        claim.task_id,
        claim.workspace_id,
        claim.document_id,
        claim.storage_backend,
        claim.storage_key,
        claim.attempt_count,
        uuid4(),
    )
    assert not _finish_success(
        test_session_factory, stale_claim, datetime.now(timezone.utc)
    )
    with test_session_factory() as db:
        assert db.get(StorageCleanupTask, task_id).status == "in_progress"
    assert _finish_success(test_session_factory, claim, datetime.now(timezone.utc))


def test_invalid_storage_key_is_never_passed_to_storage(test_session_factory):
    task_id = create_task(test_session_factory, storage_key="../untrusted.txt")

    def storage_must_not_be_resolved(*_args):
        raise AssertionError("Invalid key must be rejected before adapter selection.")

    result = process_next_cleanup_task(
        test_session_factory,
        Settings(_env_file=None),
        storage_factory=storage_must_not_be_resolved,
    )
    assert result == "permanently_failed"
    with test_session_factory() as db:
        task = db.get(StorageCleanupTask, task_id)
        assert task.last_error_code == "invalid_storage_key"
        assert task.status == "permanently_failed"


def test_worker_uses_recorded_backend_after_global_setting_changes(
    test_session_factory
):
    task_id = create_task(test_session_factory, backend="r2")
    observed_backends: list[str] = []
    storage = FakeStorage()

    def storage_factory(_settings, backend):
        observed_backends.append(backend)
        return storage

    current_settings = Settings(_env_file=None, document_storage_backend="local")
    assert process_next_cleanup_task(
        test_session_factory,
        current_settings,
        storage_factory=storage_factory,
    ) == "completed"
    assert observed_backends == ["r2"]
    with test_session_factory() as db:
        assert db.get(StorageCleanupTask, task_id).status == "completed"


def test_permanently_failed_task_is_requeued_without_changing_backend(
    client, test_session_factory, monkeypatch
):
    token, user_id, workspace_id = register(client, f"requeue-{uuid4().hex}@example.com")
    document_id, key = create_document(
        test_session_factory, workspace_id=workspace_id, uploader_id=user_id
    )
    with test_session_factory() as db:
        task = StorageCleanupTask(
            workspace_id=workspace_id,
            document_id=document_id,
            storage_backend="r2",
            storage_key=key,
            status="permanently_failed",
            attempt_count=5,
            last_error_code="storage_unavailable",
        )
        db.add(task)
        db.commit()
        task_id = task.id
    from app.api.routes import knowledge as knowledge_routes

    monkeypatch.setattr(
        knowledge_routes,
        "get_settings",
        lambda: Settings(_env_file=None, document_storage_backend="local"),
    )
    response = request_delete(client, token, workspace_id, document_id)
    assert response.status_code == 202
    with test_session_factory() as db:
        task = db.get(StorageCleanupTask, task_id)
        assert task.status == "pending"
        assert task.attempt_count == 0
        assert task.storage_backend == "r2"
        assert task.last_error_code is None


def test_admin_replays_failed_task_after_document_was_deleted_and_audits_history(
    client, test_session_factory
):
    token, operator_id, _workspace_id = register(
        client, f"admin-replay-{uuid4().hex}@example.com"
    )
    grant_admin(test_session_factory, operator_id)
    workspace_id = uuid4()
    document_id = uuid4()
    key = f"{uuid4().hex}.txt"
    task_id = create_task(
        test_session_factory,
        workspace_id=workspace_id,
        document_id=document_id,
        storage_key=key,
        backend="r2",
        status="permanently_failed",
        attempt_count=5,
    )
    with test_session_factory() as db:
        task = db.get(StorageCleanupTask, task_id)
        task.last_error_code = "storage_unavailable"
        db.commit()
        assert db.get(KnowledgeDocument, document_id) is None

    response = replay_task(client, token, task_id)

    assert response.status_code == 202
    assert response.json() == {
        "status": "pending",
        "message": "Cleanup task replay was queued.",
    }
    with test_session_factory() as db:
        task = db.get(StorageCleanupTask, task_id)
        audits = list(
            db.scalars(
                select(StorageCleanupReplayAudit).where(
                    StorageCleanupReplayAudit.task_id == task_id
                )
            )
        )
        assert task is not None
        assert task.status == "pending"
        assert task.attempt_count == 0
        assert task.storage_backend == "r2"
        assert task.storage_key == key
        assert task.next_attempt_at.replace(tzinfo=timezone.utc) <= (
            datetime.now(timezone.utc) + timedelta(seconds=1)
        )
        assert task.last_error_code is None
        assert len(audits) == 1
        audit = audits[0]
        assert audit.operator_user_id == operator_id
        assert audit.workspace_id == workspace_id
        assert audit.document_id == document_id
        assert audit.storage_backend == "r2"
        assert audit.storage_key == key
        assert audit.previous_attempt_count == 5
        assert audit.previous_error_code == "storage_unavailable"

    storage = FakeStorage()
    storage.objects.add((workspace_id, key))
    assert process_next_cleanup_task(
        test_session_factory,
        Settings(_env_file=None, document_storage_backend="local"),
        storage_factory=lambda _settings, backend: (
            storage if backend == "r2" else pytest.fail("Wrong backend selected.")
        ),
    ) == "completed"
    assert storage.delete_calls == [(workspace_id, key)]
    with test_session_factory() as db:
        assert db.get(StorageCleanupTask, task_id).status == "completed"
        assert len(
            list(
                db.scalars(
                    select(StorageCleanupReplayAudit).where(
                        StorageCleanupReplayAudit.task_id == task_id
                    )
                )
            )
        ) == 1


def test_non_admin_cannot_replay_cleanup_task(client, test_session_factory):
    token, user_id, _workspace_id = register(
        client, f"non-admin-replay-{uuid4().hex}@example.com"
    )
    task_id = create_task(
        test_session_factory,
        status="permanently_failed",
        attempt_count=5,
    )

    response = replay_task(client, token, task_id)

    assert response.status_code == 403
    with test_session_factory() as db:
        assert db.get(User, user_id).is_admin is False
        assert db.get(StorageCleanupTask, task_id).status == "permanently_failed"
        assert list(
            db.scalars(
                select(StorageCleanupReplayAudit).where(
                    StorageCleanupReplayAudit.task_id == task_id
                )
            )
        ) == []


def test_replay_rejects_task_with_active_worker_lease(client, test_session_factory):
    token, operator_id, _workspace_id = register(
        client, f"leased-replay-{uuid4().hex}@example.com"
    )
    grant_admin(test_session_factory, operator_id)
    task_id = create_task(
        test_session_factory,
        status="in_progress",
        attempt_count=1,
        lease_token=uuid4(),
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
    )

    response = replay_task(client, token, task_id)

    assert response.status_code == 409
    with test_session_factory() as db:
        assert db.get(StorageCleanupTask, task_id).status == "in_progress"
        assert list(
            db.scalars(
                select(StorageCleanupReplayAudit).where(
                    StorageCleanupReplayAudit.task_id == task_id
                )
            )
        ) == []


def test_replay_rejects_invalid_storage_key_and_backend(
    client, test_session_factory
):
    token, operator_id, _workspace_id = register(
        client, f"invalid-replay-{uuid4().hex}@example.com"
    )
    grant_admin(test_session_factory, operator_id)
    bad_key_task_id = create_task(
        test_session_factory,
        storage_key="../outside.txt",
        status="permanently_failed",
        attempt_count=3,
    )
    bad_backend_task_id = create_task(
        test_session_factory,
        status="permanently_failed",
        attempt_count=3,
    )
    engine = test_session_factory.kw["bind"]
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            update(StorageCleanupTask)
            .where(StorageCleanupTask.id == bad_backend_task_id)
            .values(storage_backend="unexpected")
        )
        connection.commit()
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")

    bad_key_response = replay_task(client, token, bad_key_task_id)
    bad_backend_response = replay_task(client, token, bad_backend_task_id)

    assert bad_key_response.status_code == 409
    assert bad_backend_response.status_code == 409
    assert "../outside.txt" not in bad_key_response.text
    assert "unexpected" not in bad_backend_response.text
    with test_session_factory() as db:
        assert db.get(StorageCleanupTask, bad_key_task_id).status == "permanently_failed"
        assert db.get(StorageCleanupTask, bad_backend_task_id).status == "permanently_failed"


def test_repeated_replay_is_rejected_without_duplicate_audit(
    client, test_session_factory
):
    token, operator_id, _workspace_id = register(
        client, f"repeated-replay-{uuid4().hex}@example.com"
    )
    grant_admin(test_session_factory, operator_id)
    task_id = create_task(
        test_session_factory,
        status="permanently_failed",
        attempt_count=2,
    )

    first = replay_task(client, token, task_id)
    second = replay_task(client, token, task_id)

    assert first.status_code == 202
    assert second.status_code == 409
    with test_session_factory() as db:
        assert db.get(StorageCleanupTask, task_id).status == "pending"
        assert len(
            list(
                db.scalars(
                    select(StorageCleanupReplayAudit).where(
                        StorageCleanupReplayAudit.task_id == task_id
                    )
                )
            )
        ) == 1
