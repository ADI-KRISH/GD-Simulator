"""Session API: creation, lifecycle, timer, topic delivery, media tokens."""

import pytest
from httpx import AsyncClient

from services.realtime.livekit import decode_access_token


async def test_only_hosts_can_create_sessions(
    client: AsyncClient, candidate_auth: dict, session_payload: dict
):
    """FR-03 is host/admin only (NFR-06 RBAC)."""
    response = await client.post("/api/v1/sessions", json=session_payload, headers=candidate_auth)
    assert response.status_code == 403


async def test_create_session_opens_a_lobby(gd_session: dict):
    """FR-03 + FR-04: creating a GD leaves it WAITING for candidates."""
    assert gd_session["state"] == "WAITING"
    assert len(gd_session["code"]) == 6
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
    payload = session_payload | {
        "rubric_weights": {
            "content": 0.5,
            "communication": 0.2,
            "leadership": 0.2,
            "collaboration": 0.2,
            "behavior": 0.1,
        }
    }
    response = await client.post("/api/v1/sessions", json=payload, headers=host_auth)
    assert response.status_code == 422
    assert "sum to 1.0" in response.text


async def test_rubric_weights_must_cover_every_dimension(
    client: AsyncClient, host_auth: dict, session_payload: dict
):
    payload = session_payload | {"rubric_weights": {"content": 1.0}}
    response = await client.post("/api/v1/sessions", json=payload, headers=host_auth)
    assert response.status_code == 422
    assert "missing rubric dimensions" in response.text


async def test_min_cannot_exceed_max_participants(
    client: AsyncClient, host_auth: dict, session_payload: dict
):
    payload = session_payload | {"min_participants": 8, "max_participants": 4}
    response = await client.post("/api/v1/sessions", json=payload, headers=host_auth)
    assert response.status_code == 422


async def test_topic_is_withheld_until_the_session_starts(
    client: AsyncClient, gd_session: dict, candidate_auth: dict
):
    """FR-11: nobody prepares ahead of the others."""
    await client.post(
        "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=candidate_auth
    )
    response = await client.get(
        f"/api/v1/sessions/{gd_session['id']}/topic", headers=candidate_auth
    )
    assert response.status_code == 200
    body = response.json()
    assert body["released"] is False
    assert body["topic"] == ""


async def test_session_cannot_start_without_the_start_condition(
    client: AsyncClient, gd_session: dict, host_auth: dict, candidate_auth: dict
):
    """FR-07: one joined-but-not-ready candidate is not enough."""
    await client.post(
        "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=candidate_auth
    )
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
    for header in (candidate_auth, second_candidate):
        joined = await client.post(
            "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=header
        )
        assert joined.status_code == 200, joined.text
        ready = await client.post(
            f"/api/v1/lobbies/{session_id}/ready", json={"ready": True}, headers=header
        )
        assert ready.status_code == 200, ready.text
    started = await client.post(f"/api/v1/sessions/{session_id}/start", headers=host_auth)
    assert started.status_code == 200, started.text
    return started.json()


async def test_start_goes_live_when_preparation_is_zero(live_session: dict):
    assert live_session["state"] == "LIVE"
    assert live_session["started_at"] is not None


async def test_topic_is_released_once_live(
    client: AsyncClient, live_session: dict, candidate_auth: dict
):
    """FR-11."""
    response = await client.get(
        f"/api/v1/sessions/{live_session['id']}/topic", headers=candidate_auth
    )
    body = response.json()
    assert body["released"] is True
    assert body["topic"].startswith("Should governments regulate")


async def test_timer_counts_the_discussion_down(
    client: AsyncClient, live_session: dict, candidate_auth: dict
):
    """FR-10: the server owns the clock."""
    response = await client.get(
        f"/api/v1/sessions/{live_session['id']}/timer", headers=candidate_auth
    )
    body = response.json()
    assert body["phase"] == "discussion"
    assert body["duration_seconds"] == 900
    assert 0 < body["seconds_remaining"] <= 900
    assert body["warning"] is False


async def test_media_token_carries_a_livekit_join_grant(
    client: AsyncClient, live_session: dict, candidate_auth: dict
):
    """FR-08. The token must actually verify and grant room access."""
    response = await client.get(
        f"/api/v1/sessions/{live_session['id']}/media-token", headers=candidate_auth
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["url"].startswith("ws://localhost:7880")
    assert body["room"] == f"gd-{live_session['id']}"

    claims = decode_access_token(body["token"])
    assert claims["sub"] == body["identity"]
    assert claims["video"]["room"] == body["room"]
    assert claims["video"]["roomJoin"] is True
    assert claims["video"]["canPublish"] is True
    assert claims["video"]["canSubscribe"] is True


async def test_media_token_is_refused_before_the_room_opens(
    client: AsyncClient, gd_session: dict, candidate_auth: dict
):
    await client.post(
        "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=candidate_auth
    )
    response = await client.get(
        f"/api/v1/sessions/{gd_session['id']}/media-token", headers=candidate_auth
    )
    assert response.status_code == 409


async def test_non_participants_cannot_read_a_session(
    client: AsyncClient, gd_session: dict, candidate_auth: dict
):
    response = await client.get(f"/api/v1/sessions/{gd_session['id']}", headers=candidate_auth)
    assert response.status_code == 403


async def test_host_can_end_the_session_early(
    client: AsyncClient, live_session: dict, host_auth: dict
):
    """Design §18: LIVE → ENDING → ENDED."""
    response = await client.post(f"/api/v1/sessions/{live_session['id']}/end", headers=host_auth)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "ENDED"
    assert response.json()["ended_at"] is not None


async def test_a_candidate_cannot_end_the_session(
    client: AsyncClient, live_session: dict, candidate_auth: dict
):
    response = await client.post(
        f"/api/v1/sessions/{live_session['id']}/end", headers=candidate_auth
    )
    assert response.status_code == 403


async def test_sessions_cannot_be_edited_once_started(
    client: AsyncClient, live_session: dict, host_auth: dict
):
    response = await client.patch(
        f"/api/v1/sessions/{live_session['id']}", json={"duration_seconds": 600}, headers=host_auth
    )
    assert response.status_code == 409


async def test_host_lists_their_own_sessions(
    client: AsyncClient, gd_session: dict, host_auth: dict
):
    response = await client.get("/api/v1/sessions?role=host", headers=host_auth)
    assert response.status_code == 200
    assert [s["id"] for s in response.json()] == [gd_session["id"]]
