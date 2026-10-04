"""Data access for the auth slice. No business rules live here."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.auth.models import PasswordResetToken, Profile, RefreshToken, User


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email.lower()))
    return result.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def create_user(session: AsyncSession, user: User, profile: Profile) -> User:
    session.add(user)
    await session.flush()
    profile.user_id = user.id
    session.add(profile)
    await session.flush()
    await session.refresh(user)
    return user


async def store_refresh_token(
    session: AsyncSession, jti: str, user_id: uuid.UUID, expires_at: datetime
) -> None:
    session.add(RefreshToken(jti=jti, user_id=user_id, expires_at=expires_at))
    await session.flush()


async def get_refresh_token(session: AsyncSession, jti: str) -> RefreshToken | None:
    return await session.get(RefreshToken, jti)


async def revoke_refresh_token(session: AsyncSession, jti: str) -> bool:
    token = await session.get(RefreshToken, jti)
    if token is None or token.revoked_at is not None:
        return False
    token.revoked_at = datetime.now(UTC)
    await session.flush()
    return True


async def revoke_all_refresh_tokens(session: AsyncSession, user_id: uuid.UUID) -> int:
    result = await session.execute(
        select(RefreshToken).where(
            RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
        )
    )
    tokens = list(result.scalars())
    now = datetime.now(UTC)
    for token in tokens:
        token.revoked_at = now
    await session.flush()
    return len(tokens)


async def store_password_reset(
    session: AsyncSession, token_hash: str, user_id: uuid.UUID, expires_at: datetime
) -> None:
    session.add(PasswordResetToken(token_hash=token_hash, user_id=user_id, expires_at=expires_at))
    await session.flush()


async def get_password_reset(session: AsyncSession, token_hash: str) -> PasswordResetToken | None:
    return await session.get(PasswordResetToken, token_hash)


async def get_profile(session: AsyncSession, user_id: uuid.UUID) -> Profile | None:
    return await session.get(Profile, user_id)
