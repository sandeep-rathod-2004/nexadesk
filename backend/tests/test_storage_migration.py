import hashlib
import json
from contextlib import contextmanager
from io import BytesIO
from uuid import UUID, uuid4

import pytest

from app.core.config import Settings
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.services.knowledge_storage import (
    DocumentStorageError,
    DocumentStorageNotFoundError,
    LocalFilesystemDocumentStorage,
    StoredDocumentMetadata,
)
from app.services.retrieval import retrieve_chunks
from app.services.storage_migration import transfer_local_documents_to_r2


class FakeDestination:
    def __init__(self):
        self.objects: dict[tuple[UUID, str], tuple[bytes, str]] = {}
        self.fail_uploads = 0
        self.corrupt_after_upload = False
        self.calls = 0

    def exists(self, _settings, workspace_id, storage_key):
        return (workspace_id, storage_key) in self.objects

    def get_metadata(self, _settings, workspace_id, storage_key):
        try:
            data, media_type = self.objects[(workspace_id, storage_key)]
        except KeyError as exc:
            raise DocumentStorageNotFoundError from exc
        return StoredDocumentMetadata(len(data), media_type, {})

    @contextmanager
    def stream(self, _settings, workspace_id, storage_key):
        try:
            data, _media_type = self.objects[(workspace_id, storage_key)]
        except KeyError as exc:
            raise DocumentStorageNotFoundError from exc
        yield BytesIO(data)

    def put_existing(
        self, source, _settings, workspace_id, storage_key, _size, media_type
    ):
        self.calls += 1
        if self.fail_uploads:
            self.fail_uploads -= 1
            raise DocumentStorageError("Temporary storage failure.")
        key = (workspace_id, storage_key)
        if key in self.objects:
            raise DocumentStorageError("Refusing to overwrite an object.")
        data = source.read()
        if self.corrupt_after_upload:
            data += b"corrupt"
        self.objects[key] = (data, media_type)


def _registered_document(
    client, test_session_factory, storage_root, name="migrate.txt"
):
    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"migration-{uuid4().hex}@example.com",
            "password": "correct horse battery staple",
            "workspace_name": "Migration test workspace",
        },
    )
    assert registration.status_code == 201
    payload = registration.json()
    workspace_id = UUID(payload["workspaces"][0]["id"])
    uploader_id = UUID(payload["user"]["id"])
    content = b"Transfer this without re-indexing. RAG source remains intact."
    storage = LocalFilesystemDocumentStorage()
    stored = storage.put(
        BytesIO(content),
        name,
        Settings(_env_file=None, knowledge_storage_path=storage_root),
        workspace_id,
    )
    with test_session_factory() as db:
        document = KnowledgeDocument(
            workspace_id=workspace_id,
            uploaded_by_id=uploader_id,
            filename=stored.filename,
            storage_key=stored.storage_key,
            storage_backend="local",
            media_type=stored.media_type,
            size_bytes=stored.size_bytes,
            embedding_provider="ollama",
            embedding_model="nomic-embed-text",
            embedding_dimensions=2,
            status="ready",
        )
        db.add(document)
        db.flush()
        db.add(
            KnowledgeChunk(
                document_id=document.id,
                workspace_id=workspace_id,
                sequence=0,
                content="Transfer this without re-indexing. RAG source remains intact.",
                source_refs=["Text file"],
                embedding=[1.0, 0.0],
            )
        )
        db.commit()
        document_id = document.id
        created_at = document.created_at
        updated_at = document.updated_at
    return (
        document_id,
        workspace_id,
        uploader_id,
        stored,
        content,
        created_at,
        updated_at,
    )


def _run(test_session_factory, settings, report, destination, **kwargs):
    with test_session_factory() as db:
        return transfer_local_documents_to_r2(
            db,
            settings,
            report,
            r2_storage=destination,
            **kwargs,
        )


def test_dry_run_does_not_contact_r2_or_modify_source_and_records_plan(
    client, test_session_factory, tmp_path
):
    storage_root = tmp_path / "source"
    storage_root.mkdir()
    (_doc_id, workspace_id, _uploader_id, stored, content, *_rest) = (
        _registered_document(client, test_session_factory, storage_root)
    )
    destination = FakeDestination()
    report_path = tmp_path / "migration.json"
    settings = Settings(_env_file=None, knowledge_storage_path=storage_root)

    report = _run(
        test_session_factory,
        settings,
        report_path,
        destination,
        dry_run=True,
    )

    assert report["summary"] == {
        "discovered": 1,
        "copied": 0,
        "verified": 0,
        "skipped": 0,
        "planned": 1,
        "failed": 0,
    }
    assert destination.calls == 0
    assert destination.objects == {}
    with LocalFilesystemDocumentStorage().open(
        settings, workspace_id, stored.storage_key
    ) as source:
        assert source.read() == content
    record = json.loads(report_path.read_text(encoding="utf-8"))["documents"]
    assert next(iter(record.values()))["status"] == "planned"


def test_successful_transfer_preserves_database_and_rag_metadata(
    client, test_session_factory, tmp_path
):
    storage_root = tmp_path / "source"
    storage_root.mkdir()
    (
        document_id,
        workspace_id,
        uploader_id,
        stored,
        content,
        created_at,
        updated_at,
    ) = _registered_document(client, test_session_factory, storage_root)
    destination = FakeDestination()
    settings = Settings(_env_file=None, knowledge_storage_path=storage_root)

    report = _run(
        test_session_factory,
        settings,
        tmp_path / "migration.json",
        destination,
        dry_run=False,
    )

    assert report["summary"]["copied"] == 1
    assert report["summary"]["verified"] == 1
    result = next(iter(report["documents"].values()))
    assert result["sha256"] == hashlib.sha256(content).hexdigest()
    assert destination.objects[(workspace_id, stored.storage_key)] == (
        content,
        "text/plain",
    )
    with test_session_factory() as db:
        document = db.get(KnowledgeDocument, document_id)
        chunks = list(db.query(KnowledgeChunk).filter_by(document_id=document_id))
        assert document is not None
        assert (
            document.id,
            document.workspace_id,
            document.uploaded_by_id,
            document.filename,
            document.storage_key,
            document.media_type,
            document.size_bytes,
            document.created_at,
            document.updated_at,
            document.embedding_provider,
            document.embedding_model,
            document.embedding_dimensions,
            document.storage_backend,
            document.status,
        ) == (
            document_id,
            workspace_id,
            uploader_id,
            stored.filename,
            stored.storage_key,
            stored.media_type,
            len(content),
            created_at,
            updated_at,
            "ollama",
            "nomic-embed-text",
            2,
            "local",
            "ready",
        )
        assert len(chunks) == 1
        assert chunks[0].content == (
            "Transfer this without re-indexing. RAG source remains intact."
        )
        assert retrieve_chunks(
            db, workspace_id, [1.0, 0.0], limit=5, min_similarity=0
        )
    with LocalFilesystemDocumentStorage().open(
        settings, workspace_id, stored.storage_key
    ) as source:
        assert source.read() == content


def test_transfer_resumes_verifies_existing_and_duplicate_execution_is_safe(
    client, test_session_factory, tmp_path
):
    storage_root = tmp_path / "source"
    storage_root.mkdir()
    (_doc_id, workspace_id, _uploader_id, stored, _content, *_rest) = (
        _registered_document(client, test_session_factory, storage_root)
    )
    destination = FakeDestination()
    settings = Settings(_env_file=None, knowledge_storage_path=storage_root)
    report_path = tmp_path / "checkpoint.json"

    first = _run(
        test_session_factory,
        settings,
        report_path,
        destination,
        dry_run=False,
    )
    second = _run(
        test_session_factory,
        settings,
        report_path,
        destination,
        dry_run=False,
    )

    assert first["summary"]["copied"] == 1
    assert second["summary"]["copied"] == 0
    assert second["summary"]["verified"] == 1
    assert second["summary"]["skipped"] == 1
    assert destination.calls == 1
    assert (workspace_id, stored.storage_key) in destination.objects


def test_transfer_retries_failure_and_reports_missing_source(
    client, test_session_factory, tmp_path
):
    storage_root = tmp_path / "source"
    storage_root.mkdir()
    (_doc_id, _workspace_id, _uploader_id, stored, *_rest) = _registered_document(
        client, test_session_factory, storage_root
    )
    destination = FakeDestination()
    destination.fail_uploads = 1
    settings = Settings(_env_file=None, knowledge_storage_path=storage_root)

    interrupted = _run(
        test_session_factory,
        settings,
        tmp_path / "retry.json",
        destination,
        dry_run=False,
        max_attempts=1,
    )
    assert interrupted["summary"]["failed"] == 1
    assert destination.objects == {}
    recovered = _run(
        test_session_factory,
        settings,
        tmp_path / "retry.json",
        destination,
        dry_run=False,
    )
    assert recovered["summary"]["copied"] == 1
    assert destination.calls == 2

    (storage_root / stored.storage_key).unlink()
    failed = _run(
        test_session_factory,
        settings,
        tmp_path / "missing.json",
        FakeDestination(),
        dry_run=False,
    )
    assert failed["summary"]["failed"] == 1
    outcome = next(iter(failed["documents"].values()))
    assert outcome["detail"] == "source_missing"


def test_transfer_does_not_overwrite_corrupt_object_or_cross_workspace_key(
    client, test_session_factory, tmp_path
):
    storage_root = tmp_path / "source"
    storage_root.mkdir()
    (_doc_id, workspace_id, _uploader_id, stored, content, *_rest) = (
        _registered_document(client, test_session_factory, storage_root)
    )
    settings = Settings(_env_file=None, knowledge_storage_path=storage_root)
    destination = FakeDestination()
    wrong_workspace = uuid4()
    destination.objects[(wrong_workspace, stored.storage_key)] = (
        content,
        "text/plain",
    )
    destination.objects[(workspace_id, stored.storage_key)] = (
        b"corrupt existing content",
        "text/plain",
    )

    report = _run(
        test_session_factory,
        settings,
        tmp_path / "corrupt.json",
        destination,
        dry_run=False,
    )

    assert report["summary"]["failed"] == 1
    assert (
        destination.objects[(workspace_id, stored.storage_key)][0]
        == b"corrupt existing content"
    )
    assert destination.calls == 0
