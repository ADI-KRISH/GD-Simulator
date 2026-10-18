"""Auth business rules (FR-01, FR-02)."""

import logging
import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from services.auth import repository as repo
from services.auth.models import Profile, User
from services.auth.schemas import (
    LoginRequest,
    ProfileUpdate,
    RegisterRequest,
    TokenPair,
)
from services.common.config import settings
from services.common.errors import AuthenticationError, ConflictError, NotFoundError
from services.common.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_token_secret,
    hash_opaque_token,
    hash_password,
    verify_password,
)
from services.common.timeutil import as_utc, utcnow

logger = logging.getLogger(__name__)


async def register(session: AsyncSession, payload: RegisterRequest) -> User:
    email = payload.email.lower()
    if await repo.get_user_by_email(session, email) is not None:
        raise ConflictError("an account with this email already exists")

    user = User(
        email=email,
        full_name=payload.full_name.strip(),
        password_hash=hash_password(payload.password),
        role=payload.role.value,
    )
    created = await repo.create_user(session, user, Profile())
    logger.info("user registered", extra={"user_id": str(created.id), "role": created.role})
    return created


async def issue_pair(session: AsyncSession, user: User) -> TokenPair:
    """A fresh access + refresh pair for a user who has proved who they are."""
    access = create_access_token(user.id, user.role)
    refresh = create_refresh_token(user.id, user.role)
    await repo.store_refresh_token(session, refresh.jti, user.id, refresh.expires_at)
    return TokenPair(
        access_token=access.token,
        refresh_token=refresh.token,
        expires_at=access.expires_at,
    )


async def login(session: AsyncSession, payload: LoginRequest) -> TokenPair:
    user = await repo.get_user_by_email(session, payload.email)
    # Hash even when the user is missing so a wrong email and a wrong password
    # take the same time — otherwise the endpoint enumerates accounts.
    password_hash = user.password_hash if user else hash_password("timing-equaliser")
    password_ok = verify_password(payload.password, password_hash)

    if user is None or not password_ok:
        raise AuthenticationError("incorrect email or password")
    if not user.is_active:
        raise AuthenticationError("account is disabled")

    return await issue_pair(session, user)


async def refresh_tokens(session: AsyncSession, refresh_token: str) -> TokenPair:
    claims = decode_token(refresh_token, "refresh")
    stored = await repo.get_refresh_token(session, claims.jti)
    if stored is None or stored.revoked_at is not None:
        raise AuthenticationError("refresh token is not valid")

    user = await repo.get_user_by_id(session, claims.subject)
    if user is None or not user.is_active:
        raise AuthenticationError("account is not available")

    # Rotate: the presented token dies with the request that used it.
    await repo.revoke_refresh_token(session, claims.jti)
    return await issue_pair(session, user)


async def logout(session: AsyncSession, refresh_token: str) -> None:
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
    user = await repo.get_user_by_email(session, email)
    if user is None:
        return None

    token = generate_token_secret()
    expires_at = utcnow() + timedelta(minutes=settings.password_reset_ttl_minutes)
    await repo.store_password_reset(session, hash_opaque_token(token), user.id, expires_at)
    logger.info("password reset requested", extra={"user_id": str(user.id)})
    return token


async def confirm_password_reset(session: AsyncSession, token: str, new_password: str) -> None:
    record = await repo.get_password_reset(session, hash_opaque_token(token))
    if record is None or record.used_at is not None:
        raise AuthenticationError("reset token is not valid")
    if as_utc(record.expires_at) < utcnow():
        raise AuthenticationError("reset token has expired")

    user = await repo.get_user_by_id(session, record.user_id)
    if user is None:
        raise NotFoundError("user no longer exists")

    user.password_hash = hash_password(new_password)
    record.used_at = utcnow()
    # A password change invalidates every existing session.
    await repo.revoke_all_refresh_tokens(session, user.id)
    await session.flush()
    logger.info("password reset completed", extra={"user_id": str(user.id)})


async def get_current_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await repo.get_user_by_id(session, user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("account is not available")
    return user


async def update_profile(session: AsyncSession, user: User, payload: ProfileUpdate) -> User:
    profile = await repo.get_profile(session, user.id)
    if profile is None:
        raise NotFoundError("profile not found")

    if payload.full_name is not None:
        user.full_name = payload.full_name.strip()
    if payload.skills is not None:
        profile.skills = payload.skills
    if payload.experience_level is not None:
        profile.experience_level = payload.experience_level.value

    await session.flush()
    await session.refresh(user)
    return user
