"""Auth dependencies: bearer-token identity and role guards (NFR-06 RBAC).

Purpose: FastAPI dependencies that turn a Bearer token into the signed-in User and enforce role
    checks.
Why needed: Every protected endpoint in every slice needs to know who is calling and whether
    they may; keeping that in one place means no route re-implements token parsing or RBAC
    (NFR-06).
Contains:
  - bearer_scheme: reads the Authorization header without raising its own error, so we control
    the 401.
  - SessionDep: shorthand that injects a database session into a route.
  - current_user: verifies the access token and loads the active account behind it.
  - CurrentUser: shorthand that injects the authenticated user into a route.
  - require_role: builds a guard that only lets users with an allowed role through (403
    otherwise).
Used by: services/ai_platform/bridge.py, services/auth/router.py, services/evaluation/router.py,
    services/lobby/router.py, services/metrics/router.py, services/notifications/router.py,
    services/practice/router.py, services/session/router.py, services/simulation.py,
    services/speech/router.py
Depends on: services/auth/__init__.py, services/auth/models.py, services/auth/service.py,
    services/common/db.py, services/common/errors.py, services/common/security.py
"""

from typing import Annotated  # attach FastAPI Depends metadata to a type

from fastapi import Depends  # dependency injection
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer  # parse Bearer header
from sqlalchemy.ext.asyncio import AsyncSession  # per-request database session

from services.auth import service  # loads the user behind a token
from services.auth.models import Role, User  # role enum and user model
from services.common.db import get_session  # session dependency
from services.common.errors import AuthenticationError, PermissionDeniedError  # 401 / 403
from services.common.security import decode_token  # verify the JWT

# auto_error=False so a missing header reaches current_user, which raises our own 401.
bearer_scheme = HTTPBearer(auto_error=False)

# Shorthand for routes: `session: SessionDep` injects a DB session.
SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def current_user(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    """Resolve the signed-in user from the bearer access token, or raise 401."""
    # No Authorization header at all: reject with our own 401 (see auto_error=False above).
    if credentials is None:
        raise AuthenticationError("missing bearer token")
    # Verify signature and expiry, and that this is an access token (not a refresh token).
    claims = decode_token(credentials.credentials, "access")
    # Load the account so a deleted or disabled user is rejected even if the token is still valid.
    return await service.get_current_user(session, claims.subject)


# Shorthand for routes: `user: CurrentUser` injects the authenticated user.
CurrentUser = Annotated[User, Depends(current_user)]


def require_role(*allowed: Role):
    """Build a dependency that only lets users with one of `allowed` roles through."""

    async def guard(user: CurrentUser) -> User:
        """Return the user if their role is allowed, else raise 403."""
        # user.role is stored as plain text, so compare against the enum values rather than the enum
        # members.
        if user.role not in {role.value for role in allowed}:
            raise PermissionDeniedError(
                f"requires one of: {', '.join(role.value for role in allowed)}"
            )
        return user

    return guard
