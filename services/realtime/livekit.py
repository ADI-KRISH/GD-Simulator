"""LiveKit access tokens for the media plane (FR-08, design §10).

This is the only file that knows LiveKit exists. It mints a join token directly
with PyJWT rather than pulling in the vendor SDK, which keeps the media-server
integration a thin, swappable interface (claude.md "keep the LLM, STT, and
media-server integrations behind thin interfaces").

Token shape verified against the LiveKit docs and the `ClaimGrants`/`VideoGrant`
definitions in livekit/node-sdks rather than from memory, as claude.md requires:
standard `iss`/`sub`/`nbf`/`exp`/`jti` claims plus a nested `video` grant, signed
HS256 with the API secret.

Purpose: Mints the signed join tokens that let a browser enter a LiveKit media room, and names
    the room for a session.
Why needed: Audio and video run through LiveKit, but only this file knows it exists; building
    the JWT directly keeps the media server a thin, swappable dependency without pulling in its
    SDK.
Contains:
  - TOKEN_TTL: how long a join token stays valid.
  - MediaGrant: what one participant may do in the room (publish, subscribe).
  - room_name: one room per session, named from the session id.
  - create_access_token: sign the HS256 JWT with the nested video grant.
  - decode_access_token: verify a token we minted (tests and local debugging).
Used by: services/session/router.py, services/tests/test_host_in_session.py,
    services/tests/test_realtime_ws.py, services/tests/test_session_api.py
Depends on: services/common/config.py, services/common/timeutil.py
"""

import uuid  # unique token ids and room naming
from dataclasses import dataclass  # MediaGrant container
from datetime import timedelta  # token lifetime

import jwt  # PyJWT: sign the LiveKit join token (HS256)

from services.common.config import settings  # LiveKit API key and secret
from services.common.timeutil import utcnow  # token issue time

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
    """Mint a signed LiveKit join token carrying the participant's room permissions."""
    # Issue and expiry times come from one clock reading.
    now = utcnow()
    # Standard JWT claims plus the LiveKit video grant.
    claims = {
        "iss": settings.livekit_api_key,
        "sub": grant.identity,
        "nbf": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "jti": str(uuid.uuid4()),
        "name": grant.display_name,
        # LiveKit reads the room permissions from this nested grant.
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
    # HS256 with the API secret, which the LiveKit server shares.
    return jwt.encode(claims, settings.livekit_api_secret, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    """Verify a token we minted. Used by tests and by the local debug endpoint."""
    # verify_aud is off because our tokens carry no audience claim.
    return jwt.decode(
        token,
        settings.livekit_api_secret,
        algorithms=["HS256"],
        options={"verify_aud": False},
    )
