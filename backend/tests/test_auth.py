from datetime import datetime, timedelta, timezone
import secrets
from uuid import uuid4

from sqlalchemy import select

from app.core.config import Settings
from app.core.security import create_access_token
from app.models.refresh_token import RefreshToken
from app.models.user import User


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


def test_refresh_rotates_tokens_and_reuse_revokes_the_token_family(client):
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
    assert replay.status_code == 401

    family_after_replay = client.post("/api/v1/auth/refresh")
    assert family_after_replay.status_code == 401


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
