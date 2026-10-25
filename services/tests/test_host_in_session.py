"""The host of a session can get into it (regression: they could not).

Everything past the lobby is gated on a participant row, which the host never
has: they create the session, they do not join it with a code. That locked the
host out of their own session's socket, timer, topic and media room.

Purpose: Regression tests proving a session's host can watch their own session (timer, topic,
    media room) even though they hold no participant seat.
Why needed: Everything past the lobby used to require a participant row, which a host never has,
    so hosts were locked out of their own live session. These tests guard the observer path
    while making sure outsiders are still refused.
Contains:
  - _go_live: helper that joins and readies candidates, then starts the session.
  - test_the_host_can_watch_their_own_session: host reads timer and topic and gets a watch-only
    media token.
  - test_a_host_who_joins_with_the_code_is_a_candidate: joining by code seats the host as
    candidate A.
  - test_an_outsider_still_cannot_read_the_timer: access for the host did not open access to
    strangers.
Used by: nothing imports it directly (run by pytest, which discovers test_*.py files)
Depends on: services/realtime/livekit.py, services/session/service.py
"""

from httpx import AsyncClient

from services.realtime.livekit import decode_access_token
from services.session.service import OBSERVER_LABEL


async def _go_live(client: AsyncClient, gd: dict, *candidates: dict) -> None:
    """Have candidates join and ready up, then start the session."""
    # Each candidate joins by code and marks themselves ready.
    for header in candidates:
        assert (
            await client.post("/api/v1/lobbies/join", json={"code": gd["code"]}, headers=header)
        ).status_code == 200
        await client.post(f"/api/v1/lobbies/{gd['id']}/ready", json={"ready": True}, headers=header)
    # Then the host starts the session (the host token travels in the _host key).
    started = await client.post(f"/api/v1/sessions/{gd['id']}/start", headers=gd["_host"])
    assert started.status_code == 200, started.text


async def test_the_host_can_watch_their_own_session(
    client: AsyncClient,
    gd_session: dict,
    host_auth: dict,
    candidate_auth: dict,
    second_candidate: dict,
):
    """A host who is not a candidate can still watch their live session."""
    # Put the session into preparation with two ready candidates.
    await _go_live(client, gd_session | {"_host": host_auth}, candidate_auth, second_candidate)
    sid = gd_session["id"]

    # Before the fix these returned 403 for the host, who has no participant row.
    for path in ("timer", "topic"):
        response = await client.get(f"/api/v1/sessions/{sid}/{path}", headers=host_auth)
        assert response.status_code == 200, response.text

    # A media token for a host who is not seated.
    token = await client.get(f"/api/v1/sessions/{sid}/media-token", headers=host_auth)
    assert token.status_code == 200, token.text
    # The host appears as the HOST observer, not as a candidate label.
    assert token.json()["identity"] == OBSERVER_LABEL
    # Watching only: nothing the host says can reach a transcript, so it must
    # not reach the room either.
    grant = decode_access_token(token.json()["token"])["video"]
    assert grant["canPublish"] is False
    assert grant["canSubscribe"] is True


async def test_a_host_who_joins_with_the_code_is_a_candidate(
    client: AsyncClient, gd_session: dict, host_auth: dict
):
    """A host who joins with the code becomes candidate A."""
    # Joining with the code puts the host in the lobby as a normal candidate.
    joined = await client.post(
        "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=host_auth
    )
    assert joined.status_code == 200, joined.text
    # The first joiner takes label A.
    assert joined.json()["participants"][0]["candidate_label"] == "A"

    # The host-candidate can then ready up like anyone else.
    ready = await client.post(
        f"/api/v1/lobbies/{gd_session['id']}/ready", json={"ready": True}, headers=host_auth
    )
    assert ready.status_code == 200, ready.text


async def test_an_outsider_still_cannot_read_the_timer(
    client: AsyncClient, gd_session: dict, candidate_auth: dict
):
    """An outsider still cannot read the session timer."""
    # A candidate who never joined this session.
    response = await client.get(
        f"/api/v1/sessions/{gd_session['id']}/timer", headers=candidate_auth
    )
    # Letting the host in did not open the session to outsiders.
    assert response.status_code == 403
