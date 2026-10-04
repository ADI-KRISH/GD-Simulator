"""Request/response shapes for the auth + profile API."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from services.auth.models import ExperienceLevel, Role


class RegisterRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=8, max_length=256)
    role: Role = Role.CANDIDATE


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=256)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_at: datetime


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    skills: list[str] = Field(default_factory=list)
    experience_level: ExperienceLevel = ExperienceLevel.STUDENT
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    improvement_areas: list[str] = Field(default_factory=list)
    sessions_completed: int = 0


class ProfileUpdate(BaseModel):
    skills: list[str] | None = None
    experience_level: ExperienceLevel | None = None
    full_name: str | None = Field(default=None, min_length=1, max_length=200)


class UserOut(BaseModel):
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
