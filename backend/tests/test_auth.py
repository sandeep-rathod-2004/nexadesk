from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import secrets
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

from fastapi import Response
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app import models as _models  # noqa: F401
from app.api.routes import auth as auth_route
from app.core.config import Settings
from app.core.security import create_access_token
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.services import performance as performance_service
from app.services.auth import create_refresh_token, rotate_refresh_token


def register(client, email="owner@example.com", workspace_name="Northstar"):
    return client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Ada Lovelace",
            "email": email,
            "password": "correct horse battery staple",
            "workspace_name": workspace_name,
        },
    )


def test_registration_creates_hashed_password_and_owner_workspace(client):
    response = register(client)

    assert response.status_code == 201
    payload = response.json()
    assert payload["user"]["email"] == "owner@example.com"
    assert payload["user"]["full_name"] == "Ada Lovelace"
    assert len(payload["workspaces"]) == 1
    assert payload["workspaces"][0]["name"] == "Northstar"
    assert payload["workspaces"][0]["role"] == "owner"
    assert "password" not in payload
    assert "password_hash" not in payload
    assert "access_token" not in payload


def test_duplicate_registration_and_login(client):
    assert register(client).status_code == 201
    assert register(client).status_code == 409

    valid = client.post(
        "/api/v1/auth/login",
        json={"email": "OWNER@example.com", "password": "correct horse battery staple"},
    )
    invalid = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "incorrect password"},
    )
    unregistered = client.post(
        "/api/v1/auth/login",
        json={"email": "not-registered@example.com", "password": "incorrect password"},
    )

    assert valid.status_code == 200
    assert valid.json()["token_type"] == "bearer"
    assert invalid.status_code == 401
    assert unregistered.status_code == 401
    assert invalid.json()["detail"] == "Incorrect email or password."
    assert (
        unregistered.json()["detail"]
        == "This email is not registered. Please register first."
    )
    assert "nexadesk_refresh=" in valid.headers["set-cookie"]
    assert "httponly" in valid.headers["set-cookie"].lower()
    assert "samesite=lax" in valid.headers["set-cookie"].lower()


def test_login_emits_correlated_timing_without_sensitive_data(client, caplog):
    assert register(client).status_code == 201
    request_id = str(uuid4())

    with caplog.at_level(logging.INFO, logger="app.auth.performance"):
        response = client.post(
            "/api/v1/auth/login",
            headers={"X-Request-ID": request_id},
            json={
                "email": "owner@example.com",
                "password": "correct horse battery staple",
            },
        )

    assert response.status_code == 200
    events = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "app.auth.performance"
    ]
    assert {event["stage"] for event in events} == {
        "request_received",
        "user_lookup_completed",
        "password_verification_completed",
        "refresh_token_created",
        "refresh_token_committed",
        "workspace_memberships_lookup_completed",
        "login_request_completed",
    }
    assert all(event["request_id"] == request_id for event in events)
    assert any(
        event["stage"] == "login_request_completed" and
        isinstance(event.get("duration_ms"), (int, float))
        for event in events
    )
    assert "correct horse battery staple" not in caplog.text
    assert "owner@example.com" not in caplog.text
    assert "$argon2" not in caplog.text


def test_login_commits_refresh_token_and_sets_cookie_when_logger_raises(
    client, test_session_factory, monkeypatch
):
    assert register(client).status_code == 201

    def fail_logging(*_args, **_kwargs):
        raise OSError("telemetry sink unavailable")

    monkeypatch.setattr(performance_service._auth_logger, "info", fail_logging)
    response = client.post(
        "/api/v1/auth/login",
        headers={"X-Request-ID": str(uuid4())},
        json={
            "email": "owner@example.com",
            "password": "correct horse battery staple",
        },
    )

    assert response.status_code == 200
    refresh_token = response.cookies.get("nexadesk_refresh")
    assert refresh_token
    token_hash = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
    with test_session_factory() as db:
        token_record = db.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
    assert token_record is not None
    assert token_record.revoked_at is None


def test_refresh_rotates_tokens_and_duplicate_refresh_reuses_the_rotation(
    client, test_session_factory
):
    assert register(client).status_code == 201
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "correct horse battery staple"},
    )
    old_cookie = login.cookies.get("nexadesk_refresh")
    assert old_cookie

    refreshed = client.post("/api/v1/auth/refresh")
    new_cookie = refreshed.cookies.get("nexadesk_refresh")
    assert refreshed.status_code == 200
    assert new_cookie and new_cookie != old_cookie
    assert refreshed.json()["user"]["email"] == "owner@example.com"

    replay = client.post(
        "/api/v1/auth/refresh",
        headers={"Cookie": f"nexadesk_refresh={old_cookie}"},
    )
    assert replay.status_code == 200
    assert replay.cookies.get("nexadesk_refresh") == new_cookie

    old_hash = hashlib.sha256(old_cookie.encode("utf-8")).hexdigest()
    with test_session_factory() as db:
        old_record = db.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == old_hash)
        )
        assert old_record is not None
        old_record.revoked_at = datetime.now(timezone.utc) - timedelta(seconds=31)
        db.commit()

    replay_after_grace = client.post(
        "/api/v1/auth/refresh",
        headers={"Cookie": f"nexadesk_refresh={old_cookie}"},
    )
    assert replay_after_grace.status_code == 401

    family_after_replay = client.post(
        "/api/v1/auth/refresh",
        headers={"Cookie": f"nexadesk_refresh={new_cookie}"},
    )
    assert family_after_replay.status_code == 401


def test_logout_with_rotated_cookie_revokes_original_and_replacement_tokens(client):
    assert register(client).status_code == 201
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "correct horse battery staple"},
    )
    original_cookie = login.cookies.get("nexadesk_refresh")
    assert login.status_code == 200
    assert original_cookie

    refreshed = client.post("/api/v1/auth/refresh")
    replacement_cookie = refreshed.cookies.get("nexadesk_refresh")
    assert refreshed.status_code == 200
    assert replacement_cookie and replacement_cookie != original_cookie

    logout_response = client.post(
        "/api/v1/auth/logout",
        headers={"Cookie": f"nexadesk_refresh={replacement_cookie}"},
    )
    assert logout_response.status_code == 204

    original_refresh = client.post(
        "/api/v1/auth/refresh",
        headers={"Cookie": f"nexadesk_refresh={original_cookie}"},
    )
    replacement_refresh = client.post(
        "/api/v1/auth/refresh",
        headers={"Cookie": f"nexadesk_refresh={replacement_cookie}"},
    )
    assert original_refresh.status_code == 401
    assert replacement_refresh.status_code == 401


def test_production_refresh_cookie_supports_cross_site_credentials(monkeypatch):
    monkeypatch.setattr(
        auth_route,
        "get_settings",
        lambda: SimpleNamespace(
            app_env="production",
            api_v1_prefix="/api/v1",
            jwt_refresh_token_days=30,
        ),
    )
    response = Response()

    auth_route._set_refresh_cookie(response, "synthetic-refresh-token")

    cookie = response.headers["set-cookie"].lower()
    assert "nexadesk_refresh=synthetic-refresh-token" in cookie
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=none" in cookie
    assert "path=/api/v1/auth" in cookie

    cleared = Response()
    auth_route._clear_refresh_cookie(cleared)
    clear_cookie = cleared.headers["set-cookie"].lower()
    assert "nexadesk_refresh=" in clear_cookie
    assert "httponly" in clear_cookie
    assert "secure" in clear_cookie
    assert "samesite=none" in clear_cookie
    assert "path=/api/v1/auth" in clear_cookie


def test_refresh_without_cookie_returns_unauthenticated(client):
    response = client.post("/api/v1/auth/refresh")

    assert response.status_code == 401
    assert response.json()["detail"] == (
        "Session refresh is invalid. Please sign in again."
    )


def test_concurrent_refresh_requests_keep_one_valid_rotated_session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'refresh-concurrency.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    barrier = Barrier(2)

    with factory() as db:
        user = User(email="concurrent@example.com", password_hash="synthetic")
        db.add(user)
        db.commit()
        token = create_refresh_token(db, user)
        db.commit()

    def rotate():
        barrier.wait()
        with factory() as db:
            result = rotate_refresh_token(db, token)
            return result[1] if result is not None else None

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            replacements = list(executor.map(lambda _index: rotate(), range(2)))

        assert replacements[0] is not None
        assert replacements[0] == replacements[1]
        with factory() as db:
            tokens = list(db.scalars(select(RefreshToken)).all())
            assert len(tokens) == 2
            assert sum(item.revoked_at is None for item in tokens) == 1
    finally:
        engine.dispose()


def test_logout_revokes_refresh_token(client):
    assert register(client).status_code == 201
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "correct horse battery staple"},
    )
    assert login.status_code == 200

    logout_response = client.post("/api/v1/auth/logout")
    assert logout_response.status_code == 204
    assert "nexadesk_refresh=" in logout_response.headers.get("set-cookie", "")
    assert client.post("/api/v1/auth/refresh").status_code == 401


def test_expired_refresh_token_is_rejected_and_revoked(
    client, test_session_factory
):
    assert register(client).status_code == 201
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "correct horse battery staple"},
    )
    assert login.status_code == 200

    with test_session_factory() as db:
        token = db.scalar(select(RefreshToken))
        assert token is not None
        token.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    expired = client.post("/api/v1/auth/refresh")
    assert expired.status_code == 401
    assert expired.json()["detail"] == "Session refresh is invalid. Please sign in again."


def test_registration_rejects_short_password(client):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "short@example.com", "password": "short"},
    )

    assert response.status_code == 422


def test_production_settings_require_an_explicit_jwt_secret():
    try:
        Settings(_env_file=None, app_env="production")
    except ValueError as error:
        assert "JWT_SECRET_KEY" in str(error)
    else:
        raise AssertionError("Production settings accepted an implicit JWT secret.")

    configured = Settings(
        _env_file=None,
        app_env="production",
        jwt_secret_key=secrets.token_urlsafe(48),
        database_url=(
            "postgresql://nexadesk:synthetic@db.example.test/nexadesk"
            "?sslmode=verify-full"
        ),
        document_storage_backend="r2",
        r2_account_id="a" * 32,
        r2_bucket_name="nexadesk-test",
        r2_access_key_id="synthetic-access-key",
        r2_secret_access_key="synthetic-secret-key",
        generation_coordination_backend="redis",
        redis_url="rediss://cache.example.test:6380/0",
        cors_origins="https://chat.example.test",
        ollama_base_url="https://ollama.example.test",
    )
    assert len(configured.jwt_secret_key.get_secret_value()) >= 64


def test_protected_endpoint_rejects_missing_invalid_and_expired_tokens(client):
    assert register(client).status_code == 201
    no_token = client.get("/api/v1/workspaces")
    invalid = client.get(
        "/api/v1/workspaces",
        headers={"Authorization": "Bearer not-a-jwt"},
    )
    expired_token = create_access_token(
        uuid4(),
        expires_delta=timedelta(seconds=-1),
    )
    expired = client.get(
        "/api/v1/workspaces",
        headers={"Authorization": f"Bearer {expired_token}"},
    )

    assert no_token.status_code == 401
    assert invalid.status_code == 401
    assert expired.status_code == 401
    assert "JWT" not in invalid.text


def test_registration_does_not_store_plaintext_password(client, test_session_factory):
    response = register(client)
    user_id = response.json()["user"]["id"]
    with test_session_factory() as db:
        user = db.scalar(select(User).where(User.email == "owner@example.com"))
        assert user is not None
        assert user.password_hash != "correct horse battery staple"
        assert user.password_hash.startswith("$argon2")
        assert user.full_name == "Ada Lovelace"
        assert str(user.id) == user_id

    login_response = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "correct horse battery staple"},
    )
    assert login_response.status_code == 200
