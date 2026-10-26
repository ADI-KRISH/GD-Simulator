"""Auth business rules (FR-01, FR-02).

Purpose: The business rules for accounts: registering, logging in, rotating tokens, resetting
    passwords and updating profiles.
Why needed: Routes stay thin and the rules (no account enumeration, token rotation, single-use
    reset) are in one testable place.
Contains:
  - register: create a user with an empty profile; reject duplicate emails.
  - issue_pair: mint and store an access plus refresh token pair.
  - login: check credentials in a way that hides whether the email exists.
  - refresh_tokens: validate, revoke and replace a refresh token (rotation).
  - logout: revoke a refresh token; an already-dead token is fine.
  - request_password_reset / confirm_password_reset: issue and consume a single-use reset token.
  - get_current_user: load the active account behind a verified token.
  - update_profile: apply only the fields the client sent.
Used by: services/ai_platform/bridge.py, services/auth/deps.py, services/auth/router.py
Depends on: services/auth/__init__.py, services/auth/models.py, services/auth/schemas.py,
    services/common/config.py, services/common/errors.py, services/common/security.py,
    services/common/timeutil.py
"""

import logging  # audit-style info logs for register/reset
import uuid  # user id type
from datetime import timedelta  # reset-token lifetime

from sqlalchemy.ext.asyncio import AsyncSession  # database session

from services.auth import repository as repo  # data access
from services.auth.models import Profile, User  # tables
from services.auth.schemas import (  # request/response shapes
    LoginRequest,
    ProfileUpdate,
    RegisterRequest,
    TokenPair,
)
from services.common.config import settings  # password-reset TTL
from services.common.errors import AuthenticationError, ConflictError, NotFoundError  # 401/409/404
from services.common.security import (  # hashing and token helpers
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_token_secret,
    hash_opaque_token,
    hash_password,
    verify_password,
)
from services.common.timeutil import as_utc, utcnow  # UTC-safe time comparisons

logger = logging.getLogger(__name__)


async def register(session: AsyncSession, payload: RegisterRequest) -> User:
    """Create a user with an empty profile; fail if the email is already taken."""
    # Normalise the email so uniqueness checks are case-insensitive.
    email = payload.email.lower()
    # Reject duplicates up front for a clean 409 instead of a database constraint error.
    if await repo.get_user_by_email(session, email) is not None:
        raise ConflictError("an account with this email already exists")

    # Build the account with a hashed password; the plaintext is never stored.
    user = User(
        email=email,
        full_name=payload.full_name.strip(),
        password_hash=hash_password(payload.password),
        role=payload.role.value,
    )
    # Create the user and an empty profile together in one transaction.
    created = await repo.create_user(session, user, Profile())
    logger.info("user registered", extra={"user_id": str(created.id), "role": created.role})
    return created


async def issue_pair(session: AsyncSession, user: User) -> TokenPair:
    """A fresh access + refresh pair for a user who has proved who they are."""
    # A short-lived access token for API calls and a longer refresh token to get new ones.
    access = create_access_token(user.id, user.role)
    refresh = create_refresh_token(user.id, user.role)
    # Persist the refresh token id so logout and rotation can revoke it later.
    await repo.store_refresh_token(session, refresh.jti, user.id, refresh.expires_at)
    return TokenPair(
        access_token=access.token,
        refresh_token=refresh.token,
        expires_at=access.expires_at,
    )


async def login(session: AsyncSession, payload: LoginRequest) -> TokenPair:
    """Check credentials and return tokens; wrong email and wrong password look identical."""
    # Look up the account; a missing one is handled below without revealing it.
    user = await repo.get_user_by_email(session, payload.email)
    # Hash even when the user is missing so a wrong email and a wrong password
    # take the same time — otherwise the endpoint enumerates accounts.
    password_hash = user.password_hash if user else hash_password("timing-equaliser")
    password_ok = verify_password(payload.password, password_hash)

    # Same error for unknown email and wrong password, so accounts cannot be enumerated.
    if user is None or not password_ok:
        raise AuthenticationError("incorrect email or password")
    # Correct credentials but a disabled account may not sign in.
    if not user.is_active:
        raise AuthenticationError("account is disabled")

    # Credentials are good: mint and store a fresh token pair.
    return await issue_pair(session, user)


async def refresh_tokens(session: AsyncSession, refresh_token: str) -> TokenPair:
    """Validate a refresh token, revoke it, and issue a new pair (token rotation)."""
    # Checks signature and expiry, and that this really is a refresh token.
    claims = decode_token(refresh_token, "refresh")
    # A valid signature is not enough: the token must still be on file and unrevoked.
    stored = await repo.get_refresh_token(session, claims.jti)
    if stored is None or stored.revoked_at is not None:
        raise AuthenticationError("refresh token is not valid")

    # The account may have been disabled since the token was issued.
    user = await repo.get_user_by_id(session, claims.subject)
    if user is None or not user.is_active:
        raise AuthenticationError("account is not available")

    # Rotate: the presented token dies with the request that used it.
    await repo.revoke_refresh_token(session, claims.jti)
    return await issue_pair(session, user)


async def logout(session: AsyncSession, refresh_token: str) -> None:
    """Revoke the refresh token; an already-invalid token is silently fine."""
    # Decode first to find the token id; an undecodable token needs no revocation.
    try:
        claims = decode_token(refresh_token, "refresh")
    except AuthenticationError:
        # Logging out with a dead token is already the desired end state.
        return
    await repo.revoke_refresh_token(session, claims.jti)


async def request_password_reset(session: AsyncSession, email: str) -> str | None:
    """Returns the reset token, or None when the email has no account.

    The caller must not leak which case happened — see the router.
    """
    # Unknown email: return None so the router can still answer identically.
    user = await repo.get_user_by_email(session, email)
    if user is None:
        return None

    # A random secret handed to the user once; only its hash is persisted.
    token = generate_token_secret()
    # Reset tokens are short-lived (settings.password_reset_ttl_minutes).
    expires_at = utcnow() + timedelta(minutes=settings.password_reset_ttl_minutes)
    # Only the hash is stored; the plaintext token goes back to the caller once.
    await repo.store_password_reset(session, hash_opaque_token(token), user.id, expires_at)
    logger.info("password reset requested", extra={"user_id": str(user.id)})
    return token


async def confirm_password_reset(session: AsyncSession, token: str, new_password: str) -> None:
    """Use a valid, unexpired, unused reset token to set a new password."""
    # Tokens are stored hashed, so hash the presented one before looking it up.
    record = await repo.get_password_reset(session, hash_opaque_token(token))
    # Unknown or already-used token: this is what makes a reset token single-use.
    if record is None or record.used_at is not None:
        raise AuthenticationError("reset token is not valid")
    # as_utc normalises naive datetimes (SQLite) before comparing with now.
    if as_utc(record.expires_at) < utcnow():
        raise AuthenticationError("reset token has expired")

    # The account could have been deleted after the reset was requested.
    user = await repo.get_user_by_id(session, record.user_id)
    if user is None:
        raise NotFoundError("user no longer exists")

    # Store the new hash and mark the token used so it cannot be replayed.
    user.password_hash = hash_password(new_password)
    record.used_at = utcnow()
    # A password change invalidates every existing session.
    await repo.revoke_all_refresh_tokens(session, user.id)
    await session.flush()
    logger.info("password reset completed", extra={"user_id": str(user.id)})


async def get_current_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    """Load the active user behind a verified token, or raise 401."""
    # Re-check the database on every request so disabling an account takes effect immediately.
    user = await repo.get_user_by_id(session, user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("account is not available")
    return user


async def update_profile(session: AsyncSession, user: User, payload: ProfileUpdate) -> User:
    """Apply the provided profile changes (only fields that are not None)."""
    # Every user is created with a profile, so a missing one is a data problem rather than a user
    # error.
    profile = await repo.get_profile(session, user.id)
    if profile is None:
        raise NotFoundError("profile not found")

    # PATCH semantics: None means leave that field unchanged.
    if payload.full_name is not None:
        user.full_name = payload.full_name.strip()
    if payload.skills is not None:
        profile.skills = payload.skills
    if payload.experience_level is not None:
        profile.experience_level = payload.experience_level.value

    # Push the changes, then refresh so the returned user reflects the saved state.
    await session.flush()
    await session.refresh(user)
    return user
