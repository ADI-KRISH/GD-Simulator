"""Session API: creation, lifecycle, timer, topic delivery, media tokens.

Purpose: End-to-end tests of the session HTTP API: creating sessions, validation of rubric
    weights and capacity, topic withholding, start conditions, timer, media tokens, ending,
    editing and listing.
Why needed: The session API drives the whole discussion lifecycle. These tests check that rules
    which must hold for fairness and security (hidden topic, host-only actions, normalised
    rubric, room closed until start) are enforced at the HTTP layer.
Contains:
  - Creation tests: students can open a lobby and take seat A, a new session opens in WAITING
    with default weights.
  - Validation tests: rubric weights must sum to 1 and cover every dimension, min cannot exceed
    max.
  - Start tests: topic is withheld until start, a session cannot start without the start
    condition.
  - live_session: fixture with two ready candidates and a started session.
  - LIVE tests: goes straight to LIVE with zero preparation, topic released, timer counts down,
    media token verifies, token refused before start.
  - Access tests: non-participants cannot read, only the host can end, started sessions cannot
    be edited, host listing.
Used by: nothing imports it directly (run by pytest, which discovers test_*.py files)
Depends on: services/realtime/livekit.py
"""

import pytest
from httpx import AsyncClient

from services.realtime.livekit import decode_access_token


async def test_students_can_create_lobbies_and_take_seat_a(
    client: AsyncClient, candidate_auth: dict, session_payload: dict
):
    """Any student can open a lobby; they sit in it as candidate A."""
    # A plain candidate (not a host) creates a lobby.
    response = await client.post("/api/v1/sessions", json=session_payload, headers=candidate_auth)
    assert response.status_code == 201
    # They are seated as candidate A.
    labels = [p["candidate_label"] for p in response.json()["participants"]]
    assert labels == ["A"]


async def test_create_session_opens_a_lobby(gd_session: dict):
    """FR-03 + FR-04: creating a GD leaves it WAITING for candidates."""
    # The gd_session fixture creates a session, which opens straight into the lobby.
    assert gd_session["state"] == "WAITING"
    # Join codes are six characters.
    assert len(gd_session["code"]) == 6
    # With no custom rubric, the default weights apply.
    assert gd_session["rubric_weights"] == {
        "content": 0.30,
        "communication": 0.20,
        "leadership": 0.20,
        "collaboration": 0.20,
        "behavior": 0.10,
    }


async def test_rubric_weights_must_sum_to_one(
    client: AsyncClient, host_auth: dict, session_payload: dict
):
    """Deterministic aggregation depends on a normalised policy (design §31)."""
    # A rubric whose weights add up to 1.1.
    payload = session_payload | {
        "rubric_weights": {
            "content": 0.5,
            "communication": 0.2,
            "leadership": 0.2,
            "collaboration": 0.2,
            "behavior": 0.1,
        }
    }
    # Rejected by the schema validator.
    response = await client.post("/api/v1/sessions", json=payload, headers=host_auth)
    assert response.status_code == 422
    # The error message explains why.
    assert "sum to 1.0" in response.text


async def test_rubric_weights_must_cover_every_dimension(
    client: AsyncClient, host_auth: dict, session_payload: dict
):
    """Custom rubric weights must include every dimension."""
    # A rubric naming only one dimension.
    payload = session_payload | {"rubric_weights": {"content": 1.0}}
    response = await client.post("/api/v1/sessions", json=payload, headers=host_auth)
    assert response.status_code == 422
    assert "missing rubric dimensions" in response.text


async def test_min_cannot_exceed_max_participants(
    client: AsyncClient, host_auth: dict, session_payload: dict
):
    """min_participants above max_participants is rejected."""
    # A minimum of 8 with a maximum of 4 is impossible.
    payload = session_payload | {"min_participants": 8, "max_participants": 4}
    response = await client.post("/api/v1/sessions", json=payload, headers=host_auth)
    assert response.status_code == 422


async def test_topic_is_withheld_until_the_session_starts(
    client: AsyncClient, gd_session: dict, candidate_auth: dict
):
    """FR-11: nobody prepares ahead of the others."""
    # A candidate joins the lobby.
    await client.post(
        "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=candidate_auth
    )
    # They ask for the topic before the session starts.
    response = await client.get(
        f"/api/v1/sessions/{gd_session['id']}/topic", headers=candidate_auth
    )
    assert response.status_code == 200
    body = response.json()
    # Withheld: released is false and the topic text is empty.
    assert body["released"] is False
    assert body["topic"] == ""


async def test_session_cannot_start_without_the_start_condition(
    client: AsyncClient, gd_session: dict, host_auth: dict, candidate_auth: dict
):
    """FR-07: one joined-but-not-ready candidate is not enough."""
    # One candidate joins but never readies.
    await client.post(
        "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=candidate_auth
    )
    # The host tries to start.
    response = await client.post(f"/api/v1/sessions/{gd_session['id']}/start", headers=host_auth)
    assert response.status_code == 409
    assert "start condition not met" in response.text


@pytest.fixture
async def live_session(
    client: AsyncClient,
    gd_session: dict,
    host_auth: dict,
    candidate_auth: dict,
    second_candidate: dict,
) -> dict:
    """Two ready candidates and a started session. Preparation is 0s in the
    fixture payload, so the session goes straight to LIVE."""
    session_id = gd_session["id"]
    # Both candidates join and ready up.
    for header in (candidate_auth, second_candidate):
        joined = await client.post(
            "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=header
        )
        assert joined.status_code == 200, joined.text
        ready = await client.post(
            f"/api/v1/lobbies/{session_id}/ready", json={"ready": True}, headers=header
        )
        assert ready.status_code == 200, ready.text
    # The host starts; preparation is zero seconds, so it goes straight to LIVE.
    started = await client.post(f"/api/v1/sessions/{session_id}/start", headers=host_auth)
    assert started.status_code == 200, started.text
    return started.json()


async def test_start_goes_live_when_preparation_is_zero(live_session: dict):
    """With no preparation the session goes straight to LIVE."""
    # The state and the start time confirm it.
    assert live_session["state"] == "LIVE"
    assert live_session["started_at"] is not None


async def test_topic_is_released_once_live(
    client: AsyncClient, live_session: dict, candidate_auth: dict
):
    """FR-11."""
    # A participant asks for the topic after the start.
    response = await client.get(
        f"/api/v1/sessions/{live_session['id']}/topic", headers=candidate_auth
    )
    body = response.json()
    assert body["released"] is True
    # The real topic text is now delivered.
    assert body["topic"].startswith("Should governments regulate")


async def test_timer_counts_the_discussion_down(
    client: AsyncClient, live_session: dict, candidate_auth: dict
):
    """FR-10: the server owns the clock."""
    # Ask the server for the clock.
    response = await client.get(
        f"/api/v1/sessions/{live_session['id']}/timer", headers=candidate_auth
    )
    body = response.json()
    # The discussion phase, 900 seconds long and partly elapsed.
    assert body["phase"] == "discussion"
    assert body["duration_seconds"] == 900
    assert 0 < body["seconds_remaining"] <= 900
    # The two-minute warning is not on yet.
    assert body["warning"] is False


async def test_media_token_carries_a_livekit_join_grant(
    client: AsyncClient, live_session: dict, candidate_auth: dict
):
    """FR-08. The token must actually verify and grant room access."""
    # Request the LiveKit credentials.
    response = await client.get(
        f"/api/v1/sessions/{live_session['id']}/media-token", headers=candidate_auth
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # They point at the local LiveKit server and this session's room.
    assert body["url"].startswith("ws://localhost:7880")
    assert body["room"] == f"gd-{live_session['id']}"

    # Decode the JWT to prove it is real and grants access.
    claims = decode_access_token(body["token"])
    assert claims["sub"] == body["identity"]
    assert claims["video"]["room"] == body["room"]
    assert claims["video"]["roomJoin"] is True
    # A seated candidate may both publish and subscribe.
    assert claims["video"]["canPublish"] is True
    assert claims["video"]["canSubscribe"] is True


async def test_media_token_is_refused_before_the_room_opens(
    client: AsyncClient, gd_session: dict, candidate_auth: dict
):
    """A media token is refused until the session has started."""
    # Join the lobby only; the session has not started.
    await client.post(
        "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=candidate_auth
    )
    # Ask for a media token too early.
    response = await client.get(
        f"/api/v1/sessions/{gd_session['id']}/media-token", headers=candidate_auth
    )
    # Refused: the room is not open yet.
    assert response.status_code == 409


async def test_non_participants_cannot_read_a_session(
    client: AsyncClient, gd_session: dict, candidate_auth: dict
):
    """A non-participant cannot read a session (403)."""
    # This candidate never joined.
    response = await client.get(f"/api/v1/sessions/{gd_session['id']}", headers=candidate_auth)
    assert response.status_code == 403


async def test_host_can_end_the_session_early(
    client: AsyncClient, live_session: dict, host_auth: dict
):
    """Design §18: LIVE → ENDING → ENDED."""
    # The host ends the session early.
    response = await client.post(f"/api/v1/sessions/{live_session['id']}/end", headers=host_auth)
    assert response.status_code == 200, response.text
    # It ends cleanly and records an end time.
    assert response.json()["state"] == "ENDED"
    assert response.json()["ended_at"] is not None


async def test_a_candidate_cannot_end_the_session(
    client: AsyncClient, live_session: dict, candidate_auth: dict
):
    """Only the host can end a session."""
    # A candidate tries to end it.
    response = await client.post(
        f"/api/v1/sessions/{live_session['id']}/end", headers=candidate_auth
    )
    assert response.status_code == 403


async def test_sessions_cannot_be_edited_once_started(
    client: AsyncClient, live_session: dict, host_auth: dict
):
    """Editing a started session is a 409."""
    # Try to shorten the duration of a live session.
    response = await client.patch(
        f"/api/v1/sessions/{live_session['id']}", json={"duration_seconds": 600}, headers=host_auth
    )
    assert response.status_code == 409


async def test_host_lists_their_own_sessions(
    client: AsyncClient, gd_session: dict, host_auth: dict
):
    """A host sees their sessions in the host listing."""
    # Ask for the sessions I host.
    response = await client.get("/api/v1/sessions?role=host", headers=host_auth)
    assert response.status_code == 200
    # Exactly the one that was created.
    assert [s["id"] for s in response.json()] == [gd_session["id"]]
