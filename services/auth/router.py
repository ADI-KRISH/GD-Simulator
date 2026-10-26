"""Auth + profile HTTP API (FR-01, FR-02).

Purpose: The HTTP endpoints under /api/v1/auth: register, login, refresh, logout, password reset
    and the signed-in user's profile.
Why needed: It is the public face of the auth slice; the frontend and every API client sign in
    and manage their account through these routes.
Contains:
  - register / login: create an account and exchange credentials for tokens.
  - refresh / logout: rotate and revoke refresh tokens.
  - request_password_reset / confirm_password_reset: two-step reset that never reveals whether
    an email exists.
  - me / update_me: read and edit the signed-in user's profile.
Used by: services/main.py
Depends on: services/auth/__init__.py, services/auth/deps.py, services/auth/schemas.py,
    services/auth/service.py, services/common/config.py
"""

from fastapi import APIRouter, status  # router and HTTP status constants

from services.auth import service  # business rules behind each endpoint
from services.auth.deps import CurrentUser, SessionDep  # injected user and DB session
from services.auth.schemas import (  # request/response bodies
    LoginRequest,
    LogoutRequest,
    PasswordResetConfirm,
    PasswordResetIssued,
    PasswordResetRequest,
    ProfileUpdate,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserOut,
)
from services.common.config import settings  # app_env decides whether to expose reset tokens

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, session: SessionDep) -> UserOut:
    """Create an account and return it."""
    # Business rules (duplicate email check, hashing) live in the service, not in the route.
    user = await service.register(session, payload)
    return UserOut.model_validate(user)


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, session: SessionDep) -> TokenPair:
    """Exchange email + password for an access/refresh token pair."""
    return await service.login(session, payload)


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, session: SessionDep) -> TokenPair:
    """Trade a refresh token for a new pair (the old refresh token is revoked)."""
    return await service.refresh_tokens(session, payload.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: LogoutRequest, session: SessionDep) -> None:
    """Revoke the given refresh token."""
    await service.logout(session, payload.refresh_token)


@router.post("/password-reset/request", response_model=PasswordResetIssued)
async def request_password_reset(
    payload: PasswordResetRequest, session: SessionDep
) -> PasswordResetIssued:
    """Start a password reset; the response never reveals whether the email exists."""
    # May be None when the email is unknown; handled below so the reply looks identical either way.
    token = await service.request_password_reset(session, payload.email)
    # Always the same response shape, so this cannot be used to discover which
    # emails have accounts. The token itself only rides along locally, where
    # there is no mail service to deliver it.
    expose_token = token if settings.app_env == "local" else None
    return PasswordResetIssued(
        detail="if that email has an account, a reset token has been issued",
        reset_token=expose_token,
    )


@router.post("/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_password_reset(payload: PasswordResetConfirm, session: SessionDep) -> None:
    """Finish a reset: set the new password using the token."""
    await service.confirm_password_reset(session, payload.token, payload.new_password)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    """Return the signed-in user and their profile."""
    return UserOut.model_validate(user)


@router.patch("/me", response_model=UserOut)
async def update_me(payload: ProfileUpdate, user: CurrentUser, session: SessionDep) -> UserOut:
    """Update the signed-in user's name, skills or experience level."""
    # Only the fields the client actually sent are changed (see ProfileUpdate).
    updated = await service.update_profile(session, user, payload)
    return UserOut.model_validate(updated)
