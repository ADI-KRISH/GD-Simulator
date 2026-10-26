"""Data access for the auth slice. No business rules live here.

Purpose: Thin database access for the auth slice: fetch and store users, profiles, refresh
    tokens and reset tokens.
Why needed: Keeping queries here lets service.py hold the business rules and be tested without
    caring how rows are fetched.
Contains:
  - get_user_by_email / get_user_by_id: find accounts for login and token checks.
  - create_user: insert a user and their profile together.
  - store/get/revoke_refresh_token and revoke_all_refresh_tokens: track which refresh tokens are
    still valid.
  - store_password_reset / get_password_reset: persist and look up hashed reset tokens.
  - get_profile: load a user's profile for editing.
Used by: services/simulation.py
Depends on: services/auth/models.py
"""

import uuid  # user ids
from datetime import UTC, datetime  # revocation timestamps

from sqlalchemy import select  # build queries
from sqlalchemy.ext.asyncio import AsyncSession  # database session

from services.auth.models import PasswordResetToken, Profile, RefreshToken, User  # tables


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    """Find a user by email (case-insensitive: emails are stored lowercase)."""
    # Emails are stored lowercase (see service.register), so lowercase the lookup too.
    result = await session.execute(select(User).where(User.email == email.lower()))
    return result.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """Find a user by primary key."""
    return await session.get(User, user_id)


async def create_user(session: AsyncSession, user: User, profile: Profile) -> User:
    """Insert a user together with their profile and return the refreshed user."""
    # Insert the user first because the profile row needs the generated id.
    session.add(user)
    await session.flush()  # assigns user.id so the profile can reference it
    # Link the profile to the new user and insert it in the same transaction.
    profile.user_id = user.id
    session.add(profile)
    await session.flush()
    # Reload so server defaults (created_at) and the selectin-loaded profile are populated.
    await session.refresh(user)
    return user


async def store_refresh_token(
    session: AsyncSession, jti: str, user_id: uuid.UUID, expires_at: datetime
) -> None:
    """Record an issued refresh token so it can later be checked or revoked."""
    session.add(RefreshToken(jti=jti, user_id=user_id, expires_at=expires_at))
    await session.flush()


async def get_refresh_token(session: AsyncSession, jti: str) -> RefreshToken | None:
    """Look up a stored refresh token by its JWT id."""
    return await session.get(RefreshToken, jti)


async def revoke_refresh_token(session: AsyncSession, jti: str) -> bool:
    """Mark one refresh token revoked; False if it is unknown or already revoked."""
    # Revoking twice is a no-op: return False so callers can tell nothing changed.
    token = await session.get(RefreshToken, jti)
    if token is None or token.revoked_at is not None:
        return False
    token.revoked_at = datetime.now(UTC)
    await session.flush()
    return True


async def revoke_all_refresh_tokens(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Revoke every live refresh token for a user and return how many were revoked."""
    # Only tokens not yet revoked need touching.
    result = await session.execute(
        select(RefreshToken).where(
            RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
        )
    )
    tokens = list(result.scalars())
    # One timestamp for the whole batch keeps the revocations consistent.
    now = datetime.now(UTC)
    for token in tokens:
        token.revoked_at = now
    await session.flush()
    return len(tokens)


async def store_password_reset(
    session: AsyncSession, token_hash: str, user_id: uuid.UUID, expires_at: datetime
) -> None:
    """Save a (hashed) password-reset token for a user."""
    session.add(PasswordResetToken(token_hash=token_hash, user_id=user_id, expires_at=expires_at))
    await session.flush()


async def get_password_reset(session: AsyncSession, token_hash: str) -> PasswordResetToken | None:
    """Look up a password-reset record by token hash."""
    return await session.get(PasswordResetToken, token_hash)


async def get_profile(session: AsyncSession, user_id: uuid.UUID) -> Profile | None:
    """Load a user's profile."""
    return await session.get(Profile, user_id)
