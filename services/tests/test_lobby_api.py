"""Lobby membership and readiness (FR-04 – FR-07, design §7.4)."""

from httpx import AsyncClient


async def test_join_by_code_assigns_a_candidate_label(
    client: AsyncClient, gd_session: dict, candidate_auth: dict
):
    """FR-05. Labels are what the AI layer sees instead of names or UUIDs."""
    response = await client.post(
        "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=candidate_auth
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["occupancy"] == 1
    assert body["participants"][0]["candidate_label"] == "A"
    assert body["participants"][0]["state"] == "JOINED"


async def test_labels_are_allocated_in_order(
    client: AsyncClient, gd_session: dict, candidate_auth: dict, second_candidate: dict
):
    for header in (candidate_auth, second_candidate):
        await client.post("/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=header)
    lobby = await client.get(f"/api/v1/lobbies/{gd_session['id']}", headers=candidate_auth)
    assert [p["candidate_label"] for p in lobby.json()["participants"]] == ["A", "B"]


async def test_rejoining_is_idempotent(client: AsyncClient, gd_session: dict, candidate_auth: dict):
    """Refreshing the lobby page must not create a second participant."""
    for _ in range(3):
        response = await client.post(
            "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=candidate_auth
        )
        assert response.status_code == 200
    assert response.json()["occupancy"] == 1


async def test_unknown_code_is_a_404(client: AsyncClient, candidate_auth: dict):
    response = await client.post(
        "/api/v1/lobbies/join", json={"code": "ZZZZZZ"}, headers=candidate_auth
    )
    assert response.status_code == 404


async def test_lobby_refuses_to_exceed_capacity(
    client: AsyncClient,
    host_auth: dict,
    session_payload: dict,
    candidate_auth: dict,
    second_candidate: dict,
):
    """FR-05 capacity check."""
    payload = session_payload | {"min_participants": 2, "max_participants": 2}
    created = await client.post("/api/v1/sessions", json=payload, headers=host_auth)
    code = created.json()["code"]

    for header in (candidate_auth, second_candidate):
        joined = await client.post("/api/v1/lobbies/join", json={"code": code}, headers=header)
        assert joined.status_code == 200

    third = {
        "email": "candidate3@example.com",
        "full_name": "Third Candidate",
        "password": "correct-horse-battery",
    }
    await client.post("/api/v1/auth/register", json=third)
    login = await client.post(
        "/api/v1/auth/login", json={"email": third["email"], "password": third["password"]}
    )
    third_auth = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = await client.post("/api/v1/lobbies/join", json={"code": code}, headers=third_auth)
    assert response.status_code == 409
    assert "full" in response.text


async def test_readiness_flips_the_session_between_waiting_and_ready(
    client: AsyncClient,
    gd_session: dict,
    candidate_auth: dict,
    second_candidate: dict,
    host_auth: dict,
):
    """FR-07 + design §8: the start condition drives WAITING ⇄ READY."""
    session_id = gd_session["id"]
    for header in (candidate_auth, second_candidate):
        await client.post("/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=header)

    await client.post(
        f"/api/v1/lobbies/{session_id}/ready", json={"ready": True}, headers=candidate_auth
    )
    lobby = await client.get(f"/api/v1/lobbies/{session_id}", headers=candidate_auth)
    assert lobby.json()["session_state"] == "WAITING"
    assert lobby.json()["start_condition_met"] is False

    await client.post(
        f"/api/v1/lobbies/{session_id}/ready", json={"ready": True}, headers=second_candidate
    )
    lobby = await client.get(f"/api/v1/lobbies/{session_id}", headers=candidate_auth)
    assert lobby.json()["session_state"] == "READY"
    assert lobby.json()["start_condition_met"] is True

    # Un-readying must take the session back out of READY.
    await client.post(
        f"/api/v1/lobbies/{session_id}/ready", json={"ready": False}, headers=second_candidate
    )
    lobby = await client.get(f"/api/v1/lobbies/{session_id}", headers=candidate_auth)
    assert lobby.json()["session_state"] == "WAITING"


async def test_leaving_frees_the_slot_and_notifies(
    client: AsyncClient, gd_session: dict, candidate_auth: dict, second_candidate: dict, events
):
    """FR-06."""
    session_id = gd_session["id"]
    for header in (candidate_auth, second_candidate):
        await client.post("/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=header)

    response = await client.post(f"/api/v1/lobbies/{session_id}/leave", headers=second_candidate)
    assert response.status_code == 204

    lobby = await client.get(f"/api/v1/lobbies/{session_id}", headers=candidate_auth)
    assert lobby.json()["occupancy"] == 1
    assert events.count("participant.left") == 1


async def test_a_candidate_who_left_cannot_rejoin(
    client: AsyncClient, gd_session: dict, candidate_auth: dict
):
    await client.post(
        "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=candidate_auth
    )
    await client.post(f"/api/v1/lobbies/{gd_session['id']}/leave", headers=candidate_auth)
    response = await client.post(
        "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=candidate_auth
    )
    assert response.status_code == 409


async def test_non_participants_cannot_read_the_lobby(
    client: AsyncClient, gd_session: dict, candidate_auth: dict
):
    response = await client.get(f"/api/v1/lobbies/{gd_session['id']}", headers=candidate_auth)
    assert response.status_code == 403


async def test_membership_changes_are_published_to_the_event_bus(
    client: AsyncClient, gd_session: dict, candidate_auth: dict, events
):
    """Design §36: services react to events, not to each other."""
    session_id = gd_session["id"]
    await client.post(
        "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=candidate_auth
    )
    await client.post(
        f"/api/v1/lobbies/{session_id}/ready", json={"ready": True}, headers=candidate_auth
    )

    assert events.count("participant.joined") == 1
    assert events.count("participant.ready") == 1
    assert events.payloads("participant.joined")[0]["candidate_label"] == "A"
