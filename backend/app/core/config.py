import ipaddress
import re
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


def _is_valid_hostname(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        try:
            ascii_hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            return False
        if (
            not ascii_hostname
            or ascii_hostname.endswith("..")
            or len(ascii_hostname) > 253
        ):
            return False
        return all(
            re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
            is not None
            for label in ascii_hostname.rstrip(".").split(".")
        )


class Settings(BaseSettings):
    app_name: str = "NexaDesk API"
    app_env: Literal["development", "test", "production"] = "development"
    api_v1_prefix: str = "/api/v1"
    database_url: str = "sqlite:///./nexadesk.db"
    generation_coordination_backend: Literal["local", "redis"] = "local"
    redis_url: str | None = None
    generation_lease_seconds: int = Field(default=60, ge=10, le=3600)
    generation_coordination_poll_seconds: float = Field(default=0.5, gt=0, le=10)
    ai_provider: Literal["ollama", "gemini"] = "ollama"
    embedding_provider: Literal["ollama", "gemini"] = "ollama"
    embedding_model: str | None = None
    gemini_api_key: SecretStr | None = None
    gemini_model: str = Field(default="gemini-2.5-flash", min_length=1)
    gemini_embedding_model: str = Field(
        default="gemini-embedding-001", min_length=1
    )
    gemini_embedding_dimensions: int = Field(default=768, ge=1, le=3072)
    gemini_timeout_seconds: float = Field(default=90.0, gt=0)
    gemini_max_retries: int = Field(default=2, ge=0, le=3)
    # Generate production values with: secrets.token_urlsafe(48)
    jwt_secret_key: SecretStr = Field(
        default_factory=lambda: SecretStr(secrets.token_urlsafe(48)),
        min_length=32,
    )
    jwt_access_token_minutes: int = Field(default=15, gt=0, le=1440)
    jwt_refresh_token_days: int = Field(default=30, gt=0, le=90)
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:1.5b"
    ollama_embedding_model: str = "nomic-embed-text"
    ollama_timeout_seconds: float = 90.0
    ollama_connect_timeout_seconds: float = Field(default=10.0, gt=0)
    ollama_read_timeout_seconds: float = Field(default=600.0, gt=0)
    ollama_keep_alive: str = "15m"
    ollama_embedding_keep_alive: str = "5m"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    max_message_chars: int = 4000
    document_storage_backend: Literal["local", "r2"] = "local"
    knowledge_storage_path: Path = Path("data/knowledge")
    r2_account_id: str | None = None
    r2_endpoint_url: str | None = None
    r2_bucket_name: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: SecretStr | None = None
    r2_region: str = "auto"
    r2_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    r2_read_timeout_seconds: float = Field(default=60.0, gt=0)
    knowledge_max_file_size_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    knowledge_max_pdf_pages: int = Field(default=300, gt=0)
    knowledge_chunk_size: int = Field(default=1200, ge=200, le=8000)
    knowledge_chunk_overlap: int = Field(default=200, ge=0, le=2000)
    knowledge_retrieval_count: int = Field(default=5, ge=1, le=20)
    knowledge_min_similarity: float = Field(default=0.35, ge=-1.0, le=1.0)

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    @field_validator("gemini_api_key", mode="before")
    @classmethod
    def empty_gemini_key_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("database_url", mode="before")
    @classmethod
    def use_psycopg_driver(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+psycopg://", 1)
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+psycopg://", 1)
        return value

    @field_validator("app_env", mode="before")
    @classmethod
    def normalize_app_env(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().lower()
            return "production" if normalized == "prod" else normalized
        return value

    @field_validator("redis_url")
    @classmethod
    def validate_redis_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return value
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("REDIS_URL must be a valid Redis URL.") from exc
        if (
            parsed.scheme not in {"redis", "rediss"}
            or not parsed.hostname
            or not _is_valid_hostname(parsed.hostname)
            or parsed.fragment
            or any(character.isspace() for character in value)
            or (parsed.path not in {"", "/"} and not re.fullmatch(r"/\d+", parsed.path))
        ):
            raise ValueError("REDIS_URL must be a valid Redis URL.")
        return value

    @field_validator("ollama_base_url")
    @classmethod
    def validate_ollama_base_url(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("OLLAMA_BASE_URL must be a valid HTTP(S) URL.") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or not _is_valid_hostname(parsed.hostname)
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or any(character.isspace() for character in value)
        ):
            raise ValueError("OLLAMA_BASE_URL must be a valid HTTP(S) URL.")
        return value

    @model_validator(mode="after")
    def require_production_jwt_secret(self) -> "Settings":
        if self.knowledge_chunk_overlap >= self.knowledge_chunk_size:
            raise ValueError(
                "KNOWLEDGE_CHUNK_OVERLAP must be smaller than KNOWLEDGE_CHUNK_SIZE."
            )
        production = self.app_env == "production"
        secret_value = self.jwt_secret_key.get_secret_value()
        lowered_secret = secret_value.casefold()
        weak_secret_markers = (
            "replace-with",
            "change-me",
            "changeme",
            "placeholder",
            "your-secret",
            "example",
            "default",
            "insecure",
            "sample",
            "secret",
            "password",
            "secret-key",
        )
        repeated_pattern = re.fullmatch(r"(.{1,8})\1+", secret_value) is not None
        if (
            any(marker in lowered_secret for marker in weak_secret_markers)
            or len(set(secret_value)) < 8
            or repeated_pattern
        ):
            raise ValueError(
                "JWT_SECRET_KEY is weak or a placeholder; generate it with "
                "secrets.token_urlsafe(48)."
            )
        if production and "jwt_secret_key" not in self.model_fields_set:
            raise ValueError("JWT_SECRET_KEY must be configured outside development.")
        if production:
            try:
                database_backend = make_url(self.database_url).get_backend_name()
            except (ArgumentError, TypeError, ValueError) as exc:
                raise ValueError("DATABASE_URL must be a valid PostgreSQL URL in production.") from exc
            if database_backend != "postgresql":
                raise ValueError("DATABASE_URL must use PostgreSQL in production.")
            sslmode = make_url(self.database_url).query.get("sslmode")
            if not isinstance(sslmode, str) or sslmode.lower() not in {
                "require",
                "verify-ca",
                "verify-full",
            }:
                raise ValueError(
                    "DATABASE_URL must configure PostgreSQL TLS with "
                    "sslmode=require, verify-ca, or verify-full in production."
                )
            if self.document_storage_backend != "r2":
                raise ValueError(
                    "DOCUMENT_STORAGE_BACKEND=r2 is required in production."
                )
            if self.generation_coordination_backend != "redis":
                raise ValueError(
                    "GENERATION_COORDINATION_BACKEND=redis is required in production."
                )
            if not self.redis_url:
                raise ValueError("REDIS_URL must be configured in production.")
            if urlsplit(self.redis_url).scheme != "rediss":
                raise ValueError("REDIS_URL must use TLS (rediss://) in production.")
            if "cors_origins" not in self.model_fields_set:
                raise ValueError("CORS_ORIGINS must be configured in production.")
            self._validate_production_origins()
            self._validate_production_ollama_endpoint()
        if (
            (self.ai_provider == "gemini" or self.embedding_provider == "gemini")
            and self.gemini_api_key is None
        ):
            raise ValueError(
                "GEMINI_API_KEY must be configured when using the Gemini provider."
            )
        if self.document_storage_backend == "r2":
            if not self.r2_endpoint_url and not self.r2_account_id:
                raise ValueError(
                    "R2_ENDPOINT_URL or R2_ACCOUNT_ID must be configured for R2 storage."
                )
            if not self.r2_bucket_name:
                raise ValueError("R2_BUCKET_NAME must be configured for R2 storage.")
            if not self.r2_access_key_id or self.r2_secret_access_key is None:
                raise ValueError(
                    "R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY are required for R2 storage."
                )
            if not self.r2_access_key_id.strip() or not self.r2_secret_access_key.get_secret_value().strip():
                raise ValueError("R2 credentials must not be empty.")
            if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.r2_bucket_name):
                raise ValueError("R2_BUCKET_NAME is invalid.")
            if self.r2_account_id and not re.fullmatch(
                r"[a-fA-F0-9]{32}", self.r2_account_id
            ):
                raise ValueError("R2_ACCOUNT_ID is invalid.")
            endpoint = self.configured_r2_endpoint
            try:
                parsed = urlsplit(endpoint)
                port = parsed.port
            except ValueError as exc:
                raise ValueError("R2 endpoint must be a valid HTTP(S) URL.") from exc
            if (
                parsed.scheme not in {"https", "http"}
                or not parsed.hostname
                or not _is_valid_hostname(parsed.hostname)
                or (port is not None and not 1 <= port <= 65535)
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise ValueError("R2 endpoint must be a valid HTTP(S) URL.")
            if (
                production and parsed.scheme != "https"
            ):
                raise ValueError("R2 endpoint must use HTTPS in production.")
        return self

    def _validate_production_origins(self) -> None:
        origins = [origin.strip() for origin in self.cors_origins.split(",")]
        if not origins or any(not origin or origin == "*" for origin in origins):
            raise ValueError("CORS_ORIGINS must contain explicit HTTPS origins.")
        for origin in origins:
            try:
                parsed = urlsplit(origin)
                _ = parsed.port
            except ValueError as exc:
                raise ValueError(
                    "CORS_ORIGINS must contain valid HTTPS origins."
                ) from exc
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or not _is_valid_hostname(parsed.hostname)
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
                or any(character.isspace() for character in origin)
            ):
                raise ValueError("CORS_ORIGINS must contain valid HTTPS origins.")

    def _validate_production_ollama_endpoint(self) -> None:
        parsed = urlsplit(self.ollama_base_url)
        if parsed.scheme == "https":
            return
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError(
                "OLLAMA_BASE_URL must use HTTPS for remote production endpoints."
            )

    @property
    def allowed_origins(self) -> list[str]:
        origins = [x.strip() for x in self.cors_origins.split(",") if x.strip()]
        if self.app_env != "production":
            for origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
                if origin not in origins:
                    origins.append(origin)
        return origins

    @property
    def configured_embedding_model(self) -> str:
        if self.embedding_model:
            return self.embedding_model
        if self.embedding_provider == "gemini":
            return self.gemini_embedding_model
        return self.ollama_embedding_model

    @property
    def configured_r2_endpoint(self) -> str:
        if self.r2_endpoint_url:
            return self.r2_endpoint_url.rstrip("/")
        if self.r2_account_id:
            return f"https://{self.r2_account_id}.r2.cloudflarestorage.com"
        return ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
