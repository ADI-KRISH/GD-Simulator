"""Request/response shapes for the auth + profile API.

Purpose: Pydantic models that validate what the auth API accepts and shape what it returns.
Why needed: Input validation at the API boundary (email format, password length) and a guarantee
    that responses never include the password hash.
Contains:
  - RegisterRequest / LoginRequest / RefreshRequest / LogoutRequest: request bodies.
  - PasswordResetRequest / PasswordResetConfirm: the two reset steps.
  - TokenPair: access and refresh tokens returned by login and refresh.
  - ProfileOut / ProfileUpdate: profile response and partial-update body.
  - UserOut: a user as returned to clients.
  - PasswordResetIssued: reset response (carries the token only in local mode).
Used by: services/ai_platform/bridge.py, services/auth/router.py, services/auth/service.py
Depends on: services/auth/models.py
"""

import uuid  # user id type
from datetime import datetime  # timestamps in responses

from pydantic import BaseModel, ConfigDict, EmailStr, Field  # validated API models

from services.auth.models import ExperienceLevel, Role  # enums reused in the API


class RegisterRequest(BaseModel):
    """Body of POST /register."""

    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)
    # Length is enforced here; the plaintext is hashed in service.register and never stored.
    password: str = Field(min_length=8, max_length=256)
    # Defaults to CANDIDATE when the client does not say.
    role: Role = Role.CANDIDATE


class LoginRequest(BaseModel):
    """Body of POST /login."""

    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class RefreshRequest(BaseModel):
    """Body of POST /refresh."""

    refresh_token: str


class LogoutRequest(BaseModel):
    """Body of POST /logout."""

    refresh_token: str


class PasswordResetRequest(BaseModel):
    """Body of POST /password-reset/request."""

    email: EmailStr


class PasswordResetConfirm(BaseModel):
    """Body of POST /password-reset/confirm."""

    token: str
    # Same length rule as registration.
    new_password: str = Field(min_length=8, max_length=256)


class TokenPair(BaseModel):
    """Access + refresh tokens returned by login and refresh."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_at: datetime  # when the access token expires


class ProfileOut(BaseModel):
    """Profile fields returned to the client (built straight from the ORM object)."""

    # from_attributes lets this model be built straight from the ORM object.
    model_config = ConfigDict(from_attributes=True)

    skills: list[str] = Field(default_factory=list)
    experience_level: ExperienceLevel = ExperienceLevel.STUDENT
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    improvement_areas: list[str] = Field(default_factory=list)
    sessions_completed: int = 0


class ProfileUpdate(BaseModel):
    """Body of PATCH /me; fields left as None are not changed."""

    skills: list[str] | None = None
    experience_level: ExperienceLevel | None = None
    full_name: str | None = Field(default=None, min_length=1, max_length=200)


class UserOut(BaseModel):
    """A user as returned by the API (never includes the password hash)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str
    role: Role
    is_active: bool
    created_at: datetime
    profile: ProfileOut | None = None


class PasswordResetIssued(BaseModel):
    """Local-only: there is no mail service, so the token is returned directly.
    A hosted build would email it instead and return nothing."""

    detail: str
    reset_token: str | None = None
