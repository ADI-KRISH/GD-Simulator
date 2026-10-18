"""Sign in with the platform's session instead of a second account (M14, wrap-up Day 4).

The platform says who a person is — Platform_Core's `ai_core.identity`, which
reads the shell's identity headers in development and will take a real
resolver later without this file changing. GD keeps its own accounts: a
platform student gets one the first time, keyed by their platform subject, and
GD's own tokens from then on. They never see a GD password; there is none they
could use.

Off unless PLATFORM_BRIDGE=1 (the platform's launcher sets it), so a standalone
GD exposes nothing new. Outside AI_AUTH_MODE=dev the platform's identity fails
closed, and so does this.
"""

import secrets
from urllib.parse import unquote

from ai_core.identity import Role as PlatformRole
from ai_core.identity import principal_from_headers
from fastapi import APIRouter, HTTPException, Request

from services.auth import repository as repo
from services.auth.deps import SessionDep
from services.auth.models import Profile, Role, User
from services.auth.schemas import TokenPair
from services.auth.service import issue_pair
from services.common.config import settings
from services.common.security import hash_password

router = APIRouter(prefix="/api/v1/auth", tags=["platform"])

#: What a platform student with no name on record is called on screen. It is
#: the shell's own fallback, and a role, not a name: the practice room speaks
#: of such a student as "the student" (see `PracticeRoom`).
UNNAMED_STUDENT = "Student"

#: Accounts made for platform students live under this domain, one per subject.
#: `.example` is reserved (RFC 2606): it never resolves, so no mail reaches
#: anyone. Not `.local`: email-validator refuses special-use names, and every
#: response carrying the address would fail validation.
PLATFORM_EMAIL_DOMAIN = "platform.example"


def platform_email(subject: str) -> str:
    return f"{subject.strip().lower()}@{PLATFORM_EMAIL_DOMAIN}"


def platform_subject(email: str) -> str | None:
    """The platform subject behind a GD account, if it came from the platform."""
    local, _, domain = (email or "").rpartition("@")
    return local if domain == PLATFORM_EMAIL_DOMAIN and local else None


@router.post("/platform", response_model=TokenPair)
async def sign_in_with_platform(request: Request, db: SessionDep) -> TokenPair:
    if not settings.platform_bridge:
        raise HTTPException(status_code=404, detail="platform sign-in is not enabled here")
    principal = principal_from_headers(dict(request.headers))
    if principal is None or principal.role is not PlatformRole.student:
        raise HTTPException(
            status_code=401, detail="the platform did not say which student you are"
        )
    subject = principal.candidate_id or principal.subject
    # URI-encoded by GD's /platform page: a header is Latin-1, a name need not be.
    name = unquote(request.headers.get("x-display-name") or "").strip()[:200] or UNNAMED_STUDENT
    user = await repo.get_user_by_email(db, platform_email(subject))
    if user is None:
        user = await repo.create_user(
            db,
            User(
                email=platform_email(subject),
                full_name=name,
                # Unusable by design: a platform student signs in through the platform.
                password_hash=hash_password(secrets.token_urlsafe(32)),
                role=Role.CANDIDATE.value,
            ),
            Profile(),
        )
    elif name != UNNAMED_STUDENT and user.full_name != name:
        user.full_name = name
    return await issue_pair(db, user)
