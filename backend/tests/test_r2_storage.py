from io import BytesIO
from uuid import UUID, uuid4

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from sqlalchemy import select

from app.core.config import Settings
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.services.retrieval import retrieve_chunks
from app.services.knowledge_storage import (
    DocumentStorageError,
    DocumentStorageNotFoundError,
)
from app.services.r2_storage import CloudflareR2DocumentStorage


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.last_upload_rolled_to_disk = False
        self.fail_upload = False

    def upload_fileobj(self, source, bucket, key, ExtraArgs, Config):
        self.last_upload_rolled_to_disk = bool(getattr(source, "_rolled", False))
        if self.fail_upload:
            raise EndpointConnectionError(endpoint_url="https://private-endpoint")
        chunks = []
        while chunk := source.read(64 * 1024):
            chunks.append(chunk)
        self.objects[(bucket, key)] = (
            b"".join(chunks),
            ExtraArgs["ContentType"],
            ExtraArgs["Metadata"],
        )

    def get_object(self, Bucket, Key):
        content, _content_type, _metadata = self.objects[(Bucket, Key)]
        return {"Body": BytesIO(content)}

    def head_object(self, Bucket, Key):
        try:
            content, content_type, metadata = self.objects[(Bucket, Key)]
        except KeyError as exc:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadObject",
            ) from exc
        return {
            "ContentLength": len(content),
            "ContentType": content_type,
            "Metadata": metadata,
        }

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


def r2_settings(**overrides) -> Settings:
    options = {
        "document_storage_backend": "r2",
        "r2_endpoint_url": "https://test-account.r2.cloudflarestorage.com",
        "r2_bucket_name": "nexadesk-private",
        "r2_access_key_id": "test-access",
        "r2_secret_access_key": "test-secret-not-a-real-credential",
    }
    options.update(overrides)
    return Settings(_env_file=None, **options)


def test_r2_upload_download_metadata_existence_and_delete(monkeypatch):
    settings = r2_settings()
    workspace_id = uuid4()
    client = FakeS3Client()
    monkeypatch.setattr("app.services.r2_storage._get_client", lambda _settings: client)
    storage = CloudflareR2DocumentStorage()

    stored = storage.put(
        BytesIO(b"private RAG content"),
        r"..\private\notes.txt",
        settings,
        workspace_id,
    )
    object_key = f"workspaces/{workspace_id}/documents/{stored.storage_key}"
    assert stored.filename == "notes.txt"
    assert stored.size_bytes == len(b"private RAG content")
    assert client.objects[("nexadesk-private", object_key)][2] == {
        "workspace-id": str(workspace_id)
    }
    assert storage.exists(settings, workspace_id, stored.storage_key)

    with storage.open(settings, workspace_id, stored.storage_key) as source:
        assert source.read() == b"private RAG content"
    with storage.stream(settings, workspace_id, stored.storage_key) as source:
        assert source.read() == b"private RAG content"
    metadata = storage.get_metadata(settings, workspace_id, stored.storage_key)
    assert metadata.size_bytes == stored.size_bytes
    assert metadata.media_type == "text/plain"
    assert metadata.metadata == {"workspace-id": str(workspace_id)}

    storage.delete(settings, workspace_id, stored.storage_key)
    assert not storage.exists(settings, workspace_id, stored.storage_key)
    with pytest.raises(DocumentStorageNotFoundError):
        storage.get_metadata(settings, workspace_id, stored.storage_key)


def test_r2_keys_are_workspace_scoped_and_reject_user_controlled_paths(
    monkeypatch,
):
    settings = r2_settings()
    client = FakeS3Client()
    monkeypatch.setattr("app.services.r2_storage._get_client", lambda _settings: client)
    storage = CloudflareR2DocumentStorage()
    workspace_a = uuid4()
    workspace_b = uuid4()
    stored = storage.put(BytesIO(b"tenant A"), "a.txt", settings, workspace_a)

    assert storage.exists(settings, workspace_a, stored.storage_key)
    assert not storage.exists(settings, workspace_b, stored.storage_key)
    for operation in (
        lambda: storage.delete(settings, workspace_a, "../escape.txt"),
        lambda: storage.exists(settings, workspace_a, "../escape.txt"),
        lambda: storage.get_metadata(settings, workspace_a, "../escape.txt"),
    ):
        with pytest.raises(ValueError, match="Invalid document storage key"):
            operation()
    with pytest.raises(ValueError, match="Invalid document storage key"):
        with storage.open(settings, workspace_a, "../escape.txt"):
            pass


def test_r2_upload_spools_large_documents_and_maps_provider_errors_safely(
    monkeypatch,
):
    settings = r2_settings(knowledge_max_file_size_bytes=3 * 1024 * 1024)
    client = FakeS3Client()
    monkeypatch.setattr("app.services.r2_storage._get_client", lambda _settings: client)
    storage = CloudflareR2DocumentStorage()
    content = b"x" * (2 * 1024 * 1024)

    storage.put(BytesIO(content), "large.txt", settings, uuid4())

    assert client.last_upload_rolled_to_disk

    client.fail_upload = True
    with pytest.raises(DocumentStorageError) as error:
        storage.put(BytesIO(b"small"), "small.txt", settings, uuid4())
    assert "private-endpoint" not in str(error.value)
    assert "test-secret-not-a-real-credential" not in str(error.value)


def test_r2_temporary_provider_unavailability_is_safe_for_all_storage_operations(
    monkeypatch,
):
    settings = r2_settings()

    class UnavailableS3Client:
        @staticmethod
        def _fail():
            raise EndpointConnectionError(
                endpoint_url="https://provider-secret@private-provider.invalid/"
            )

        def upload_fileobj(self, *_args, **_kwargs):
            self._fail()

        def put_object(self, **_kwargs):
            self._fail()

        def get_object(self, **_kwargs):
            self._fail()

        def head_object(self, **_kwargs):
            self._fail()

        def delete_object(self, **_kwargs):
            self._fail()

    monkeypatch.setattr(
        "app.services.r2_storage._get_client",
        lambda _settings: UnavailableS3Client(),
    )
    storage = CloudflareR2DocumentStorage()
    workspace_id = uuid4()
    storage_key = f"{uuid4().hex}.txt"

    def open_document():
        with storage.open(settings, workspace_id, storage_key):
            pass

    def stream_document():
        with storage.stream(settings, workspace_id, storage_key):
            pass

    operations = (
        lambda: storage.put(BytesIO(b"synthetic text"), "unavailable.txt", settings, workspace_id),
        lambda: storage.put_existing(
            BytesIO(b"synthetic text"),
            settings,
            workspace_id,
            storage_key,
            len(b"synthetic text"),
            "text/plain",
        ),
        open_document,
        stream_document,
        lambda: storage.get_metadata(settings, workspace_id, storage_key),
        lambda: storage.exists(settings, workspace_id, storage_key),
        lambda: storage.delete(settings, workspace_id, storage_key),
    )

    for operation in operations:
        with pytest.raises(DocumentStorageError) as error:
            operation()
        assert str(error.value) == "Document storage is unavailable."
        assert "private-provider.invalid" not in str(error.value)
        assert "provider-secret" not in str(error.value)
        assert "test-secret-not-a-real-credential" not in str(error.value)


def test_r2_configuration_requires_credentials_bucket_and_valid_endpoint():
    with pytest.raises(ValueError, match="R2_BUCKET_NAME"):
        r2_settings(r2_bucket_name=None)
    with pytest.raises(ValueError, match="R2_ACCESS_KEY_ID"):
        r2_settings(r2_secret_access_key=None)
    with pytest.raises(ValueError, match="R2 endpoint"):
        r2_settings(r2_endpoint_url="not-a-url")
    with pytest.raises(ValueError, match="R2_BUCKET_NAME is invalid"):
        r2_settings(r2_bucket_name="../private")


def test_r2_migration_put_is_workspace_scoped_and_conditionally_non_overwriting(
    monkeypatch,
):
    settings = r2_settings()
    workspace_id = uuid4()
    storage_key = f"{uuid4().hex}.txt"
    captured = {}

    class ConditionalClient:
        def put_object(self, **kwargs):
            captured.update(kwargs)
            assert kwargs["Body"].read() == b"legacy file"

    monkeypatch.setattr(
        "app.services.r2_storage._get_client",
        lambda _settings: ConditionalClient(),
    )
    CloudflareR2DocumentStorage().put_existing(
        BytesIO(b"legacy file"),
        settings,
        workspace_id,
        storage_key,
        len(b"legacy file"),
        "text/plain",
    )
    assert captured["Key"] == f"workspaces/{workspace_id}/documents/{storage_key}"
    assert captured["IfNoneMatch"] == "*"
    assert "ACL" not in captured
    assert captured["Metadata"] == {"workspace-id": str(workspace_id)}

    class AlreadyExistsClient:
        def put_object(self, **_kwargs):
            raise ClientError(
                {
                    "Error": {"Code": "PreconditionFailed"},
                    "ResponseMetadata": {"HTTPStatusCode": 412},
                },
                "PutObject",
            )

    monkeypatch.setattr(
        "app.services.r2_storage._get_client",
        lambda _settings: AlreadyExistsClient(),
    )
    from app.services.r2_storage import DocumentDestinationExistsError

    with pytest.raises(DocumentDestinationExistsError):
        CloudflareR2DocumentStorage().put_existing(
            BytesIO(b"legacy file"),
            settings,
            workspace_id,
            storage_key,
            len(b"legacy file"),
            "text/plain",
        )


def test_r2_selection_runs_document_ingestion_retrieval_download_and_cleanup(
    client, test_session_factory, monkeypatch
):
    settings = r2_settings(knowledge_chunk_size=200, knowledge_chunk_overlap=20)
    s3_client = FakeS3Client()
    monkeypatch.setattr(
        "app.services.r2_storage._get_client", lambda _settings: s3_client
    )
    from app.api.routes import knowledge as knowledge_routes

    async def deterministic_embeddings(_settings, texts, request_id=None):
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(knowledge_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(knowledge_routes, "embed_texts", deterministic_embeddings)
    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"r2-pipeline-{uuid4().hex}@example.com",
            "password": "correct horse battery staple",
            "workspace_name": "R2 pipeline workspace",
        },
    )
    assert registration.status_code == 201
    authenticated = client.post(
        "/api/v1/auth/login",
        json={
            "email": registration.json()["user"]["email"],
            "password": "correct horse battery staple",
        },
    )
    assert authenticated.status_code == 200
    token = authenticated.json()["access_token"]
    workspace_id = registration.json()["workspaces"][0]["id"]
    headers = {"Authorization": f"Bearer {token}"}

    uploaded = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents",
        headers=headers,
        files={"file": ("travel.txt", b"Reimbursement is capped at $500.")},
    )
    assert uploaded.status_code == 201
    result = uploaded.json()
    assert result["status"] == "ready"
    key = f"workspaces/{workspace_id}/documents/"
    assert any(object_key.startswith(key) for _bucket, object_key in s3_client.objects)

    with test_session_factory() as db:
        chunks = retrieve_chunks(
            db, UUID(workspace_id), [1.0, 0.0], limit=5, min_similarity=0.1
        )
        stored_document = db.get(KnowledgeDocument, UUID(result["id"]))
        assert stored_document is not None
        assert stored_document.embedding_dimensions == 2
        assert chunks and "capped at $500" in chunks[0].content

    downloaded = client.get(
        f"/api/v1/workspaces/{workspace_id}/documents/{result['id']}/download",
        headers=headers,
    )
    assert downloaded.status_code == 200
    assert downloaded.content == b"Reimbursement is capped at $500."

    deleted = client.delete(
        f"/api/v1/workspaces/{workspace_id}/documents/{result['id']}",
        headers=headers,
    )
    assert deleted.status_code == 202
    assert deleted.json()["storage_cleanup"] == "pending"
    assert s3_client.objects
    with test_session_factory() as db:
        assert db.get(KnowledgeDocument, UUID(result["id"])) is None
        assert list(db.scalars(select(KnowledgeChunk))) == []
    from app.workers.storage_cleanup import process_next_cleanup_task
    from app.services.knowledge_storage import get_document_storage_for_backend

    assert process_next_cleanup_task(
        test_session_factory,
        settings,
        storage_factory=get_document_storage_for_backend,
    ) == "completed"
    assert not s3_client.objects
