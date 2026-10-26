"""Deterministic unit tests for hashing and token handling.

Purpose:    prove that password hashing and JWT creation/verification behave
            safely: salted, no truncation, and tokens rejected when expired,
            forged or used for the wrong purpose.
Why needed: these functions guard every account; a regression here would be a
            security hole, so the rules are pinned by tests.
Contains:
    test_password_roundtrip, test_hash_is_salted - hashing works and is salted.
    test_long_passwords_are_not_truncated        - the SHA-256 pre-hash protects
        passwords longer than bcrypt's 72 bytes.
    test_verify_rejects_malformed_hash           - a bad stored hash is "wrong", not a crash.
    test_opaque_token_hash_is_stable_and_hex     - reset tokens hash deterministically.
    test_access_token_roundtrip, test_refresh_token_carries_distinct_jti - token contents.
    test_token_type_is_enforced                  - refresh cannot pass as access.
    test_expired_token_is_rejected, test_token_signed_with_another_key_is_rejected -
        expiry and forgery are refused.
Used by:    pytest.
Depends on: common/config, common/errors, common/security.
"""

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
    """A hashed password verifies with the right password and fails with a wrong one."""
    # The stored value must not be the plaintext, yet still verify correctly.
    hashed = hash_password("correct-horse-battery")
    assert hashed != "correct-horse-battery"
    assert verify_password("correct-horse-battery", hashed)
    assert not verify_password("wrong-password", hashed)


def test_hash_is_salted():
    """Hashing the same password twice gives different hashes."""
    assert hash_password("same-input") != hash_password("same-input")


def test_long_passwords_are_not_truncated():
    """bcrypt ignores bytes past 72; the sha256 pre-hash must prevent that."""
    # Two passwords identical for 72 bytes but different after; both must matter.
    base = "a" * 72
    hashed = hash_password(base + "TAIL-ONE")
    assert verify_password(base + "TAIL-ONE", hashed)
    assert not verify_password(base + "TAIL-TWO", hashed)


def test_verify_rejects_malformed_hash():
    """A malformed hash verifies as False instead of raising."""
    assert not verify_password("anything", "not-a-bcrypt-hash")


def test_opaque_token_hash_is_stable_and_hex():
    """Opaque token hashing is deterministic 64-character hex."""
    digest = hash_opaque_token("reset-token")
    assert digest == hash_opaque_token("reset-token")
    assert len(digest) == 64
    assert digest != hash_opaque_token("other-token")


def test_access_token_roundtrip():
    """An access token decodes back to its subject, role and id."""
    # Issue then decode: every claim we put in must come back out unchanged.
    user_id = uuid.uuid4()
    issued = create_access_token(user_id, "candidate")
    claims = decode_token(issued.token, "access")
    assert claims.subject == user_id
    assert claims.role == "candidate"
    assert claims.jti == issued.jti


def test_refresh_token_carries_distinct_jti():
    """Each refresh token gets its own jti."""
    # Unique ids are what let a single refresh token be revoked on its own.
    user_id = uuid.uuid4()
    first = create_refresh_token(user_id, "candidate")
    second = create_refresh_token(user_id, "candidate")
    assert first.jti != second.jti


def test_token_type_is_enforced():
    """A refresh token cannot be decoded as an access token."""
    issued = create_refresh_token(uuid.uuid4(), "candidate")
    with pytest.raises(AuthenticationError):
        decode_token(issued.token, "access")


def test_expired_token_is_rejected():
    """An expired token raises AuthenticationError."""
    # Hand-build a token that expired a minute ago, signed with the real secret.
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
    """A token signed with a different secret is rejected."""
    # A forged "admin" token signed with the wrong key must not be trusted.
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
