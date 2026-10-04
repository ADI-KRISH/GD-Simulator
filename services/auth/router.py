"""Auth + profile HTTP API (FR-01, FR-02)."""

from fastapi import APIRouter, status

from services.auth import service
from services.auth.deps import CurrentUser, SessionDep
from services.auth.schemas import (
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
from services.common.config import settings

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, session: SessionDep) -> UserOut:
    user = await service.register(session, payload)
    return UserOut.model_validate(user)


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, session: SessionDep) -> TokenPair:
    return await service.login(session, payload)


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, session: SessionDep) -> TokenPair:
    return await service.refresh_tokens(session, payload.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: LogoutRequest, session: SessionDep) -> None:
    await service.logout(session, payload.refresh_token)


@router.post("/password-reset/request", response_model=PasswordResetIssued)
async def request_password_reset(
    payload: PasswordResetRequest, session: SessionDep
) -> PasswordResetIssued:
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
    await service.confirm_password_reset(session, payload.token, payload.new_password)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.patch("/me", response_model=UserOut)
async def update_me(payload: ProfileUpdate, user: CurrentUser, session: SessionDep) -> UserOut:
    updated = await service.update_profile(session, user, payload)
    return UserOut.model_validate(updated)
