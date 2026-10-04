"""Password hashing and JWT issue/verify (FR-01, NFR-06)."""

import base64
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import bcrypt
import jwt

from services.common.config import settings
from services.common.errors import AuthenticationError

TokenType = Literal["access", "refresh"]


def _prehash(password: str) -> bytes:
    """bcrypt silently truncates input past 72 bytes, so hash to a fixed
    64-byte digest first. Any password length is then honoured in full."""
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt(rounds=settings.bcrypt_rounds)
    return bcrypt.hashpw(_prehash(password), salt).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode("utf-8"))
    except ValueError:
        return False


def generate_token_secret(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_opaque_token(token: str) -> str:
    """Password-reset tokens are stored hashed, never in plaintext."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IssuedToken:
    token: str
    jti: str
    expires_at: datetime


@dataclass(frozen=True)
class TokenClaims:
    subject: uuid.UUID
    role: str
    token_type: TokenType
    jti: str
    expires_at: datetime


def _issue(subject: uuid.UUID, role: str, token_type: TokenType, ttl: timedelta) -> IssuedToken:
    now = datetime.now(UTC)
    expires_at = now + ttl
    jti = str(uuid.uuid4())
    payload = {
        "sub": str(subject),
        "role": role,
        "type": token_type,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return IssuedToken(token=token, jti=jti, expires_at=expires_at)


def create_access_token(subject: uuid.UUID, role: str) -> IssuedToken:
    return _issue(subject, role, "access", timedelta(minutes=settings.access_token_ttl_minutes))


def create_refresh_token(subject: uuid.UUID, role: str) -> IssuedToken:
    return _issue(subject, role, "refresh", timedelta(days=settings.refresh_token_ttl_days))


def decode_token(token: str, expected_type: TokenType) -> TokenClaims:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("invalid token") from exc

    if payload.get("type") != expected_type:
        raise AuthenticationError(f"expected a {expected_type} token")

    try:
        subject = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("invalid token subject") from exc

    return TokenClaims(
        subject=subject,
        role=payload.get("role", "candidate"),
        token_type=expected_type,
        jti=payload.get("jti", ""),
        expires_at=datetime.fromtimestamp(payload["exp"], UTC),
    )
