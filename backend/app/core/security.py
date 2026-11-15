from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt
from pwdlib import PasswordHash
from pydantic import SecretStr
from jwt.exceptions import InvalidTokenError

from app.core.config import Settings, get_settings

_password_hash = PasswordHash.recommended()
_DUMMY_PASSWORD_HASH = _password_hash.hash("constant timing-safe dummy password")


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return _password_hash.verify(password, password_hash)


def create_access_token(
    subject: UUID,
    expires_delta: timedelta | None = None,
    settings: Settings | None = None,
) -> str:
    current_settings = settings or get_settings()
    now = datetime.now(timezone.utc)
    lifetime = (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=current_settings.jwt_access_token_minutes)
    )
    payload = {
        "sub": str(subject),
        "iat": now,
        "exp": now + lifetime,
        "iss": "nexadesk",
    }
    return jwt.encode(payload, _secret_value(current_settings.jwt_secret_key), algorithm="HS256")


def decode_access_token(token: str, settings: Settings | None = None) -> UUID:
    current_settings = settings or get_settings()
    payload = jwt.decode(
        token,
        _secret_value(current_settings.jwt_secret_key),
        algorithms=["HS256"],
        issuer="nexadesk",
        options={"require": ["sub", "exp", "iat", "iss"]},
    )
    return UUID(payload["sub"])


def _secret_value(secret: SecretStr) -> str:
    return secret.get_secret_value()


def verify_unknown_user_password(password: str) -> None:
    _password_hash.verify(password, _DUMMY_PASSWORD_HASH)
