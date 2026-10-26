"""Password hashing and JWT issue/verify (FR-01, NFR-06).

Purpose:    hash and check passwords, and create/verify the signed tokens
            (access, refresh) that prove who a caller is.
Why needed: sign-in, every protected API route and the WebSocket handshake rely
            on these; getting them wrong (plaintext passwords, tokens accepted
            for the wrong purpose) would compromise every account.
Contains:
    _prehash / hash_password / verify_password - bcrypt hashing that honours
        passwords longer than bcrypt's 72-byte limit.
    generate_token_secret / hash_opaque_token  - random reset tokens, stored
        only as hashes.
    IssuedToken, TokenClaims - small containers for a new token and a verified one.
    _issue, create_access_token, create_refresh_token - sign short-lived access
        and long-lived refresh JWTs.
    decode_token - verify signature, expiry and token type, or raise.
Used by:    ai_platform/bridge, auth/deps and service, practice/router,
            realtime/ws, simulation, tests/test_security.
Depends on: common/config (secret, TTLs, bcrypt rounds), common/errors
            (AuthenticationError).
"""

import base64  # encode the SHA-256 digest into bcrypt-safe ASCII
import hashlib  # SHA-256 for password pre-hash and opaque token hashing
import secrets  # cryptographically secure random token generation
import uuid  # user ids and token ids (jti)
from dataclasses import dataclass  # lightweight immutable token containers
from datetime import UTC, datetime, timedelta  # token issue/expiry times
from typing import Literal  # restrict token type to "access" | "refresh"

import bcrypt  # slow salted password hashing
import jwt  # PyJWT: sign and verify JSON Web Tokens

from services.common.config import settings  # JWT secret/algorithm, TTLs, bcrypt rounds
from services.common.errors import AuthenticationError  # raised on any bad token

TokenType = Literal["access", "refresh"]


def _prehash(password: str) -> bytes:
    """bcrypt silently truncates input past 72 bytes, so hash to a fixed
    64-byte digest first. Any password length is then honoured in full."""
    # SHA-256 then base64 keeps the input ASCII and always under bcrypt's limit.
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    """Return a salted bcrypt hash of the password for storage."""
    # A fresh random salt per password, with the configured work factor.
    salt = bcrypt.gensalt(rounds=settings.bcrypt_rounds)
    return bcrypt.hashpw(_prehash(password), salt).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Check a password against a stored hash; a malformed hash counts as a mismatch."""
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode("utf-8"))
    except ValueError:
        # A corrupt stored hash must read as "wrong password", not a server error.
        return False


def generate_token_secret(nbytes: int = 32) -> str:
    """Make a random URL-safe secret (used for password-reset tokens)."""
    return secrets.token_urlsafe(nbytes)


def hash_opaque_token(token: str) -> str:
    """Password-reset tokens are stored hashed, never in plaintext."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IssuedToken:
    """A freshly signed JWT plus the id and expiry we need to store or return."""

    token: str
    jti: str
    expires_at: datetime


@dataclass(frozen=True)
class TokenClaims:
    """The verified contents of a decoded JWT."""

    subject: uuid.UUID
    role: str
    token_type: TokenType
    jti: str
    expires_at: datetime


def _issue(subject: uuid.UUID, role: str, token_type: TokenType, ttl: timedelta) -> IssuedToken:
    """Sign a JWT for `subject` that expires after `ttl`."""
    now = datetime.now(UTC)
    expires_at = now + ttl
    jti = str(uuid.uuid4())  # unique id so a refresh token can be revoked individually
    # Standard claims (sub, iat, exp) plus our role and token type.
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
    """Issue a short-lived access token for API calls."""
    return _issue(subject, role, "access", timedelta(minutes=settings.access_token_ttl_minutes))


def create_refresh_token(subject: uuid.UUID, role: str) -> IssuedToken:
    """Issue a long-lived refresh token used to get new access tokens."""
    return _issue(subject, role, "refresh", timedelta(days=settings.refresh_token_ttl_days))


def decode_token(token: str, expected_type: TokenType) -> TokenClaims:
    """Verify signature, expiry and type of a JWT; raise AuthenticationError if any fails."""
    # Signature and expiry are checked by PyJWT; map its errors to ours.
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("invalid token") from exc

    # An access token must never be accepted where a refresh token is expected, or vice versa.
    if payload.get("type") != expected_type:
        raise AuthenticationError(f"expected a {expected_type} token")

    # The subject must be a real user id, otherwise treat the token as forged.
    try:
        subject = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("invalid token subject") from exc

    # Hand back typed, verified claims instead of the raw dict.
    return TokenClaims(
        subject=subject,
        role=payload.get("role", "candidate"),
        token_type=expected_type,
        jti=payload.get("jti", ""),
        expires_at=datetime.fromtimestamp(payload["exp"], UTC),
    )
