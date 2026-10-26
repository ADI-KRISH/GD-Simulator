"""Lobby membership and readiness (FR-04 – FR-07, design §7.4).

Purpose: Tests for lobby membership and readiness: joining by code, label allocation, idempotent
    re-join, capacity, the WAITING and READY flip, leaving and event publication.
Why needed: The lobby decides who is in a discussion and when it may start (FR-04 to FR-07);
    these tests protect the rules that keep seats, labels and the start condition consistent.
Contains:
  - Join tests: first joiner gets label A, labels are allocated in order, re-joining is
    idempotent, unknown code is 404.
  - Capacity test: a full lobby refuses another joiner with 409.
  - Readiness test: the start condition moves the session between WAITING and READY and back.
  - Leave tests: leaving frees the seat and publishes an event; someone who left cannot rejoin.
  - Access test: non-participants cannot read the lobby.
  - Event test: membership changes are published to the event bus (design §36).
Used by: nothing imports it directly (run by pytest, which discovers test_*.py files)
Depends on: nothing inside the project (standard library / third-party only)
"""

from httpx import AsyncClient


async def test_join_by_code_assigns_a_candidate_label(
    client: AsyncClient, gd_session: dict, candidate_auth: dict
):
    """FR-05. Labels are what the AI layer sees instead of names or UUIDs."""
    # Join with the session code from the gd_session fixture.
    response = await client.post(
        "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=candidate_auth
    )
    assert response.status_code == 200, response.text
    # The first joiner is label A, state JOINED, occupancy 1.
    body = response.json()
    assert body["occupancy"] == 1
    assert body["participants"][0]["candidate_label"] == "A"
    assert body["participants"][0]["state"] == "JOINED"


async def test_labels_are_allocated_in_order(
    client: AsyncClient, gd_session: dict, candidate_auth: dict, second_candidate: dict
):
    """Joiners get labels A, B, ... in order."""
    # Two different users join in sequence.
    for header in (candidate_auth, second_candidate):
        await client.post("/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=header)
    # Read the roster back; the order must be A then B.
    lobby = await client.get(f"/api/v1/lobbies/{gd_session['id']}", headers=candidate_auth)
    assert [p["candidate_label"] for p in lobby.json()["participants"]] == ["A", "B"]


async def test_rejoining_is_idempotent(client: AsyncClient, gd_session: dict, candidate_auth: dict):
    """Refreshing the lobby page must not create a second participant."""
    # Join three times, like three page refreshes.
    for _ in range(3):
        response = await client.post(
            "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=candidate_auth
        )
        assert response.status_code == 200
    # Still exactly one seat.
    assert response.json()["occupancy"] == 1


async def test_unknown_code_is_a_404(client: AsyncClient, candidate_auth: dict):
    """Joining with an unknown code gives 404."""
    # A code that does not exist.
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
    # A lobby with room for only two people.
    payload = session_payload | {"min_participants": 2, "max_participants": 2}
    # The host creates it.
    created = await client.post("/api/v1/sessions", json=payload, headers=host_auth)
    code = created.json()["code"]

    # Fill both seats.
    for header in (candidate_auth, second_candidate):
        joined = await client.post("/api/v1/lobbies/join", json={"code": code}, headers=header)
        assert joined.status_code == 200

    # A third user, registered on the fly.
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

    # The third join must be refused as full.
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
    # Two candidates join.
    for header in (candidate_auth, second_candidate):
        await client.post("/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=header)

    # Only one of two is ready: the start condition is not met and the session stays WAITING.
    await client.post(
        f"/api/v1/lobbies/{session_id}/ready", json={"ready": True}, headers=candidate_auth
    )
    lobby = await client.get(f"/api/v1/lobbies/{session_id}", headers=candidate_auth)
    assert lobby.json()["session_state"] == "WAITING"
    assert lobby.json()["start_condition_met"] is False

    # Both are ready: the session flips to READY.
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
    # Two candidates are in the lobby.
    for header in (candidate_auth, second_candidate):
        await client.post("/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=header)

    # The second candidate leaves.
    response = await client.post(f"/api/v1/lobbies/{session_id}/leave", headers=second_candidate)
    assert response.status_code == 204

    # Their seat is freed (occupancy 1).
    lobby = await client.get(f"/api/v1/lobbies/{session_id}", headers=candidate_auth)
    assert lobby.json()["occupancy"] == 1
    # The event bus received exactly one participant.left.
    assert events.count("participant.left") == 1


async def test_a_candidate_who_left_cannot_rejoin(
    client: AsyncClient, gd_session: dict, candidate_auth: dict
):
    """A candidate who left cannot rejoin."""
    # Join, then leave.
    await client.post(
        "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=candidate_auth
    )
    await client.post(f"/api/v1/lobbies/{gd_session['id']}/leave", headers=candidate_auth)
    # Coming back after leaving is a conflict.
    response = await client.post(
        "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=candidate_auth
    )
    assert response.status_code == 409


async def test_non_participants_cannot_read_the_lobby(
    client: AsyncClient, gd_session: dict, candidate_auth: dict
):
    """A non-participant cannot read the lobby (403)."""
    # candidate_auth never joined, so they hold no seat.
    response = await client.get(f"/api/v1/lobbies/{gd_session['id']}", headers=candidate_auth)
    assert response.status_code == 403


async def test_membership_changes_are_published_to_the_event_bus(
    client: AsyncClient, gd_session: dict, candidate_auth: dict, events
):
    """Design §36: services react to events, not to each other."""
    session_id = gd_session["id"]
    # A join and a ready action.
    await client.post(
        "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=candidate_auth
    )
    await client.post(
        f"/api/v1/lobbies/{session_id}/ready", json={"ready": True}, headers=candidate_auth
    )

    # Each action published exactly one event.
    assert events.count("participant.joined") == 1
    assert events.count("participant.ready") == 1
    # The payload carries the assigned label.
    assert events.payloads("participant.joined")[0]["candidate_label"] == "A"
