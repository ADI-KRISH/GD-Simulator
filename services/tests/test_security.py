"""Deterministic unit tests for hashing and token handling."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from services.common.config import settings
from services.common.errors import AuthenticationError
from services.common.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_opaque_token,
    hash_password,
    verify_password,
)


def test_password_roundtrip():
    hashed = hash_password("correct-horse-battery")
    assert hashed != "correct-horse-battery"
    assert verify_password("correct-horse-battery", hashed)
    assert not verify_password("wrong-password", hashed)


def test_hash_is_salted():
    assert hash_password("same-input") != hash_password("same-input")


def test_long_passwords_are_not_truncated():
    """bcrypt ignores bytes past 72; the sha256 pre-hash must prevent that."""
    base = "a" * 72
    hashed = hash_password(base + "TAIL-ONE")
    assert verify_password(base + "TAIL-ONE", hashed)
    assert not verify_password(base + "TAIL-TWO", hashed)


def test_verify_rejects_malformed_hash():
    assert not verify_password("anything", "not-a-bcrypt-hash")


def test_opaque_token_hash_is_stable_and_hex():
    digest = hash_opaque_token("reset-token")
    assert digest == hash_opaque_token("reset-token")
    assert len(digest) == 64
    assert digest != hash_opaque_token("other-token")


def test_access_token_roundtrip():
    user_id = uuid.uuid4()
    issued = create_access_token(user_id, "candidate")
    claims = decode_token(issued.token, "access")
    assert claims.subject == user_id
    assert claims.role == "candidate"
    assert claims.jti == issued.jti


def test_refresh_token_carries_distinct_jti():
    user_id = uuid.uuid4()
    first = create_refresh_token(user_id, "candidate")
    second = create_refresh_token(user_id, "candidate")
    assert first.jti != second.jti


def test_token_type_is_enforced():
    issued = create_refresh_token(uuid.uuid4(), "candidate")
    with pytest.raises(AuthenticationError):
        decode_token(issued.token, "access")


def test_expired_token_is_rejected():
    expired = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "role": "candidate",
            "type": "access",
            "jti": str(uuid.uuid4()),
            "exp": int((datetime.now(UTC) - timedelta(minutes=1)).timestamp()),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(AuthenticationError):
        decode_token(expired, "access")


def test_token_signed_with_another_key_is_rejected():
    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "role": "admin",
            "type": "access",
            "jti": str(uuid.uuid4()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        },
        "a-different-secret",
        algorithm="HS256",
    )
    with pytest.raises(AuthenticationError):
        decode_token(forged, "access")
