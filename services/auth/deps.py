"""Auth dependencies: bearer-token identity and role guards (NFR-06 RBAC)."""

from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from services.auth import service
from services.auth.models import Role, User
from services.common.db import get_session
from services.common.errors import AuthenticationError, PermissionDeniedError
from services.common.security import decode_token

bearer_scheme = HTTPBearer(auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def current_user(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    if credentials is None:
        raise AuthenticationError("missing bearer token")
    claims = decode_token(credentials.credentials, "access")
    return await service.get_current_user(session, claims.subject)


CurrentUser = Annotated[User, Depends(current_user)]


def require_role(*allowed: Role):
    async def guard(user: CurrentUser) -> User:
        if user.role not in {role.value for role in allowed}:
            raise PermissionDeniedError(
                f"requires one of: {', '.join(role.value for role in allowed)}"
            )
        return user

    return guard
