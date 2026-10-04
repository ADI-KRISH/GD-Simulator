"""The host of a session can get into it (regression: they could not).

Everything past the lobby is gated on a participant row, which the host never
has: they create the session, they do not join it with a code. That locked the
host out of their own session's socket, timer, topic and media room.
"""

from httpx import AsyncClient

from services.realtime.livekit import decode_access_token
from services.session.service import OBSERVER_LABEL


async def _go_live(client: AsyncClient, gd: dict, *candidates: dict) -> None:
    for header in candidates:
        assert (
            await client.post("/api/v1/lobbies/join", json={"code": gd["code"]}, headers=header)
        ).status_code == 200
        await client.post(f"/api/v1/lobbies/{gd['id']}/ready", json={"ready": True}, headers=header)
    started = await client.post(f"/api/v1/sessions/{gd['id']}/start", headers=gd["_host"])
    assert started.status_code == 200, started.text


async def test_the_host_can_watch_their_own_session(
    client: AsyncClient,
    gd_session: dict,
    host_auth: dict,
    candidate_auth: dict,
    second_candidate: dict,
):
    await _go_live(client, gd_session | {"_host": host_auth}, candidate_auth, second_candidate)
    sid = gd_session["id"]

    for path in ("timer", "topic"):
        response = await client.get(f"/api/v1/sessions/{sid}/{path}", headers=host_auth)
        assert response.status_code == 200, response.text

    token = await client.get(f"/api/v1/sessions/{sid}/media-token", headers=host_auth)
    assert token.status_code == 200, token.text
    assert token.json()["identity"] == OBSERVER_LABEL
    # Watching only: nothing the host says can reach a transcript, so it must
    # not reach the room either.
    grant = decode_access_token(token.json()["token"])["video"]
    assert grant["canPublish"] is False
    assert grant["canSubscribe"] is True


async def test_a_host_who_joins_with_the_code_is_a_candidate(
    client: AsyncClient, gd_session: dict, host_auth: dict
):
    joined = await client.post(
        "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=host_auth
    )
    assert joined.status_code == 200, joined.text
    assert joined.json()["participants"][0]["candidate_label"] == "A"

    ready = await client.post(
        f"/api/v1/lobbies/{gd_session['id']}/ready", json={"ready": True}, headers=host_auth
    )
    assert ready.status_code == 200, ready.text


async def test_an_outsider_still_cannot_read_the_timer(
    client: AsyncClient, gd_session: dict, candidate_auth: dict
):
    response = await client.get(
        f"/api/v1/sessions/{gd_session['id']}/timer", headers=candidate_auth
    )
    assert response.status_code == 403
