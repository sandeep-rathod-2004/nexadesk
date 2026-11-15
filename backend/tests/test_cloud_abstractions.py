import os
import secrets
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool

from app.core.config import Settings
from app.db import session as db_session
from app.services.knowledge_storage import (
    DocumentStorage,
    LocalFilesystemDocumentStorage,
)
from app.services.model_provider import (
    AIProvider,
    OllamaProvider,
    create_ai_provider,
    create_embedding_provider,
)


def production_settings_kwargs(**overrides):
    settings = {
        "_env_file": None,
        "app_env": "production",
        "database_url": (
            "postgresql://nexadesk:synthetic@db.example.test/nexadesk"
            "?sslmode=verify-full"
        ),
        "document_storage_backend": "r2",
        "r2_account_id": "a" * 32,
        "r2_bucket_name": "nexadesk-test",
        "r2_access_key_id": "synthetic-access-key",
        "r2_secret_access_key": "synthetic-secret-key",
        "generation_coordination_backend": "redis",
        "redis_url": "rediss://cache.example.test:6380/0",
        "cors_origins": "https://chat.example.test",
        "ollama_base_url": "https://ollama.example.test",
        "jwt_secret_key": secrets.token_urlsafe(48),
    }
    settings.update(overrides)
    return settings


def test_database_url_defaults_to_sqlite_and_normalizes_postgresql_urls(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert Settings(_env_file=None).database_url == "sqlite:///./nexadesk.db"
    assert (
        Settings(
            _env_file=None,
            database_url="postgres://user:pass@localhost/nexadesk",
        ).database_url
        == "postgresql+psycopg://user:pass@localhost/nexadesk"
    )
    assert (
        Settings(
            _env_file=None,
            database_url="postgresql://user:pass@localhost/nexadesk",
        ).database_url
        == "postgresql+psycopg://user:pass@localhost/nexadesk"
    )
    assert (
        Settings(
            _env_file=None,
            database_url="postgresql+psycopg://localhost/nexadesk",
        ).database_url
        == "postgresql+psycopg://localhost/nexadesk"
    )


def test_sqlite_engine_and_session_factory_use_sqlite_settings(monkeypatch, tmp_path):
    database_path = tmp_path / "session-test.db"
    monkeypatch.setattr(
        db_session, "get_settings", lambda: Settings(database_url=f"sqlite:///{database_path}")
    )
    db_session.get_session_factory.cache_clear()
    db_session.get_engine.cache_clear()
    engine = None
    try:
        engine = db_session.get_engine()
        factory = db_session.get_session_factory()
        assert engine.dialect.name == "sqlite"
        assert Path(engine.url.database).resolve() == database_path.resolve()
        with factory() as db:
            assert db.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        db_session.get_session_factory.cache_clear()
        db_session.get_engine.cache_clear()
        if engine is not None:
            engine.dispose()


def test_postgresql_engine_uses_psycopg_and_bounded_pool(monkeypatch):
    monkeypatch.setattr(
        db_session,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            database_url="postgresql://user:pass@localhost/nexadesk",
        ),
    )
    db_session.get_session_factory.cache_clear()
    db_session.get_engine.cache_clear()
    engine = None
    try:
        engine = db_session.get_engine()
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "psycopg"
        assert isinstance(engine.pool, QueuePool)
        assert engine.pool.size() == 5
        assert engine.pool._max_overflow == 10
    finally:
        db_session.get_session_factory.cache_clear()
        db_session.get_engine.cache_clear()
        if engine is not None:
            engine.dispose()


def test_database_dependency_closes_each_session(monkeypatch):
    session = Mock()
    monkeypatch.setattr(db_session, "get_session_factory", lambda: lambda: session)
    dependency = db_session.get_db()
    assert next(dependency) is session
    dependency.close()
    session.close.assert_called_once_with()


def test_generation_coordination_configuration_requires_redis_in_production():
    with pytest.raises(ValueError, match="GENERATION_COORDINATION_BACKEND=redis"):
        Settings(**production_settings_kwargs(generation_coordination_backend="local"))

    configured = Settings(**production_settings_kwargs())
    assert configured.generation_coordination_backend == "redis"
    assert configured.generation_lease_seconds == 60


@pytest.mark.parametrize(
    ("value", "normalized"),
    [
        ("development", "development"),
        ("DEVELOPMENT", "development"),
        ("test", "test"),
        ("Test", "test"),
        ("production", "production"),
        ("PRODUCTION", "production"),
        ("prod", "production"),
        (" PROD ", "production"),
    ],
)
def test_app_env_accepts_documented_values_case_insensitively(value, normalized):
    settings = Settings(**production_settings_kwargs(app_env=value))
    assert settings.app_env == normalized


@pytest.mark.parametrize("value", ["", "staging", "production-like", "dev "])
def test_app_env_rejects_unknown_values(value):
    with pytest.raises(ValueError, match="app_env"):
        Settings(_env_file=None, app_env=value)


def test_production_requires_postgresql_and_accepts_it():
    with pytest.raises(ValueError, match="DATABASE_URL must use PostgreSQL"):
        Settings(**production_settings_kwargs(database_url="sqlite:///production.db"))

    configured = Settings(**production_settings_kwargs())
    assert configured.database_url.startswith("postgresql+psycopg://")


@pytest.mark.parametrize("sslmode", ["require", "verify-ca", "verify-full", "VERIFY-FULL"])
def test_production_postgresql_accepts_secure_tls_modes(sslmode):
    settings = Settings(
        **production_settings_kwargs(
            database_url=(
                "postgresql://nexadesk:synthetic@db.example.test/nexadesk"
                f"?sslmode={sslmode}"
            )
        )
    )
    assert f"sslmode={sslmode}" in settings.database_url


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://nexadesk:synthetic@db.example.test/nexadesk",
        "postgresql://nexadesk:synthetic@db.example.test/nexadesk?sslmode=disable",
        "postgresql://nexadesk:synthetic@db.example.test/nexadesk?sslmode=allow",
        "postgresql://nexadesk:synthetic@db.example.test/nexadesk?sslmode=prefer",
    ],
)
def test_production_postgresql_rejects_missing_or_weak_tls_modes(database_url):
    with pytest.raises(ValueError, match="DATABASE_URL must configure PostgreSQL TLS"):
        Settings(**production_settings_kwargs(database_url=database_url))


def test_production_requires_r2_and_development_keeps_local_default():
    with pytest.raises(ValueError, match="DOCUMENT_STORAGE_BACKEND=r2"):
        Settings(**production_settings_kwargs(document_storage_backend="local"))

    assert Settings(_env_file=None).document_storage_backend == "local"
    assert (
        Settings(
            _env_file=None,
            database_url="sqlite:///./nexadesk.db",
        ).database_url
        == "sqlite:///./nexadesk.db"
    )


def test_production_requires_explicit_strong_jwt_secret():
    settings = production_settings_kwargs()
    settings.pop("jwt_secret_key")
    with pytest.raises(ValueError, match="JWT_SECRET_KEY must be configured"):
        Settings(**settings)


@pytest.mark.parametrize(
    "weak_secret",
    [
        "replace-with-" + "x" * 40,
        "your-secret-" + "x" * 40,
        "a" * 48,
        "abcd" * 12,
        "password" * 6,
    ],
)
def test_rejects_weak_and_placeholder_jwt_secrets(weak_secret):
    with pytest.raises(ValueError, match="JWT_SECRET_KEY is weak or a placeholder"):
        Settings(_env_file=None, jwt_secret_key=weak_secret)


def test_generated_jwt_secret_is_accepted_in_production():
    settings = Settings(**production_settings_kwargs())
    assert len(settings.jwt_secret_key.get_secret_value()) >= 32


def test_redis_url_validation_and_production_tls_requirement():
    with pytest.raises(ValueError, match="REDIS_URL must use TLS"):
        Settings(**production_settings_kwargs(redis_url="redis://cache.example.test/0"))

    configured = Settings(**production_settings_kwargs())
    assert configured.redis_url == "rediss://cache.example.test:6380/0"
    assert Settings(_env_file=None, redis_url="redis://localhost:6379/0").redis_url
    with pytest.raises(ValueError, match="REDIS_URL must be a valid Redis URL"):
        Settings(_env_file=None, redis_url="https://cache.example.test/0")

    secret_url = "redis://user:private-password@cache.example.test/0"
    with pytest.raises(ValidationError) as error:
        Settings(**production_settings_kwargs(redis_url=secret_url))
    assert "private-password" not in str(error.value)
    with pytest.raises(ValueError, match="REDIS_URL must be a valid Redis URL"):
        Settings(_env_file=None, redis_url="redis://bad host:6379/0")


def test_production_requires_explicit_https_cors_origins():
    settings = production_settings_kwargs()
    settings.pop("cors_origins")
    with pytest.raises(ValueError, match="CORS_ORIGINS must be configured"):
        Settings(**settings)

    configured = Settings(**production_settings_kwargs())
    assert configured.allowed_origins == ["https://chat.example.test"]


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "http://chat.example.test",
        "https://chat.example.test/path",
        "https://chat.example.test?query=1",
        "https://user:pass@chat.example.test",
        "https://bad_host.example.test",
        "https://chat..example.test",
        "https://chat.example.test..",
        "not-an-origin",
    ],
)
def test_production_rejects_unsafe_cors_origins(origin):
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        Settings(**production_settings_kwargs(cors_origins=origin))


def test_production_ollama_requires_https_for_remote_hosts():
    with pytest.raises(ValueError, match="OLLAMA_BASE_URL must use HTTPS"):
        Settings(**production_settings_kwargs(ollama_base_url="http://ollama.example.test"))

    remote = Settings(**production_settings_kwargs())
    local = Settings(
        **production_settings_kwargs(ollama_base_url="http://127.0.0.1:11434")
    )
    assert remote.ollama_base_url == "https://ollama.example.test"
    assert local.ollama_base_url == "http://127.0.0.1:11434"
    assert Settings(_env_file=None).ollama_base_url == "http://127.0.0.1:11434"


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://ollama.example.test",
        "http://",
        "https://user:pass@ollama.example.test",
        "https://ollama.example.test?secret=value",
        "https://ollama.example.test#fragment",
        "https://ollama.example.test:invalid",
        "https://bad_host.example.test",
    ],
)
def test_ollama_rejects_malformed_endpoints(endpoint):
    with pytest.raises(ValueError, match="OLLAMA_BASE_URL must be a valid HTTP"):
        Settings(_env_file=None, ollama_base_url=endpoint)


def test_postgresql_test_database_connection_when_configured():
    database_url = os.environ.get("TEST_POSTGRESQL_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRESQL_URL is not configured.")

    settings = Settings(_env_file=None, database_url=database_url)
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        **db_session._engine_options(settings.database_url),
    )
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT 1")) == 1
    finally:
        engine.dispose()


def test_provider_interface_and_ollama_selection():
    settings = Settings(_env_file=None, ollama_model="phi3:mini")
    provider = create_ai_provider(settings)
    embedding_provider = create_embedding_provider(settings)

    assert isinstance(provider, AIProvider)
    assert isinstance(provider, OllamaProvider)
    assert provider.model_info() == {"provider": "Ollama", "model": "phi3:mini"}
    assert isinstance(embedding_provider, AIProvider)
    assert isinstance(embedding_provider, OllamaProvider)
    assert settings.configured_embedding_model == "nomic-embed-text"


def test_filesystem_storage_interface_preserves_metadata_and_operations(tmp_path):
    settings = Settings(_env_file=None, knowledge_storage_path=tmp_path)
    storage: DocumentStorage = LocalFilesystemDocumentStorage()
    workspace_id = uuid4()
    stored = storage.put(BytesIO(b"private notes"), "notes.txt", settings, workspace_id)

    assert stored.filename == "notes.txt"
    assert stored.extension == ".txt"
    assert stored.size_bytes == len(b"private notes")
    assert stored.media_type == "text/plain"
    assert storage.exists(settings, workspace_id, stored.storage_key)
    with storage.open(settings, workspace_id, stored.storage_key) as source:
        assert source.read() == b"private notes"
    assert storage.get_metadata(
        settings, workspace_id, stored.storage_key
    ).size_bytes == len(b"private notes")

    storage.delete(settings, workspace_id, stored.storage_key)
    assert not storage.exists(settings, workspace_id, stored.storage_key)


@pytest.mark.parametrize(
    ("app_env", "endpoint"),
    [
        ("development", "http://127.0.0.1:9000"),
        ("production", "https://objects.example.test:443"),
    ],
)
def test_r2_endpoint_accepts_valid_ports(app_env, endpoint):
    settings = {
        "_env_file": None,
        "app_env": app_env,
        "document_storage_backend": "r2",
        "r2_endpoint_url": endpoint,
        "r2_bucket_name": "nexadesk-test",
        "r2_access_key_id": "synthetic-access-key",
        "r2_secret_access_key": "synthetic-secret-key",
    }
    if app_env == "production":
        settings.update(production_settings_kwargs())
        settings["r2_endpoint_url"] = endpoint
    configured = Settings(**settings)
    assert configured.configured_r2_endpoint == endpoint


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://objects.example.test:invalid",
        "https://objects.example.test:65536",
        "https://objects.example.test:0",
    ],
)
def test_r2_endpoint_rejects_invalid_ports(endpoint):
    with pytest.raises(ValueError, match="R2 endpoint must be a valid HTTP"):
        Settings(
            _env_file=None,
            document_storage_backend="r2",
            r2_endpoint_url=endpoint,
            r2_bucket_name="nexadesk-test",
            r2_access_key_id="synthetic-access-key",
            r2_secret_access_key="synthetic-secret-key",
        )
