"""LiveKit access tokens for the media plane (FR-08, design §10).

This is the only file that knows LiveKit exists. It mints a join token directly
with PyJWT rather than pulling in the vendor SDK, which keeps the media-server
integration a thin, swappable interface (claude.md "keep the LLM, STT, and
media-server integrations behind thin interfaces").

Token shape verified against the LiveKit docs and the `ClaimGrants`/`VideoGrant`
definitions in livekit/node-sdks rather than from memory, as claude.md requires:
standard `iss`/`sub`/`nbf`/`exp`/`jti` claims plus a nested `video` grant, signed
HS256 with the API secret.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta

import jwt

from services.common.config import settings
from services.common.timeutil import utcnow

# A join token only needs to outlive the session it was minted for. Long enough
# to cover a full GD plus reconnects, short enough that a leaked one expires.
TOKEN_TTL = timedelta(hours=6)


@dataclass(frozen=True, slots=True)
class MediaGrant:
    """What a participant may do on the media plane."""

    room: str
    identity: str
    display_name: str
    can_publish: bool = True
    can_subscribe: bool = True


def room_name(session_id: uuid.UUID | str) -> str:
    """One LiveKit room per GD session, named by session id."""
    return f"gd-{session_id}"


def create_access_token(grant: MediaGrant, ttl: timedelta = TOKEN_TTL) -> str:
    now = utcnow()
    claims = {
        "iss": settings.livekit_api_key,
        "sub": grant.identity,
        "nbf": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "jti": str(uuid.uuid4()),
        "name": grant.display_name,
        "video": {
            "room": grant.room,
            "roomJoin": True,
            "canPublish": grant.can_publish,
            "canSubscribe": grant.can_subscribe,
            # Data messages carry nothing authoritative — control events go over
            # the WebSocket plane (design §10) — but the client uses them for
            # low-latency UI hints like active-speaker echo.
            "canPublishData": True,
        },
    }
    return jwt.encode(claims, settings.livekit_api_secret, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    """Verify a token we minted. Used by tests and by the local debug endpoint."""
    return jwt.decode(
        token,
        settings.livekit_api_secret,
        algorithms=["HS256"],
        options={"verify_aud": False},
    )
