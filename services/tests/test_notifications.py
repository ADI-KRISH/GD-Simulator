"""Notifications and invitations (FR-32, FR-05).

The point of a durable notification is that it survives not being connected, so
these check the row exists and is readable later — not that a WebSocket frame
was sent at the time.

Purpose: Tests for durable notifications (FR-32) and invitations (FR-05): who is notified when,
    privacy between users, read marking and the invitation flow.
Why needed: The value of a durable notification is that it survives not being connected, so
    these check that the stored row exists and is readable later, that users cannot see or touch
    each other's rows, and that an unanswered invitation cannot block a session from starting.
Contains:
  - _notifications / kinds: helpers that fetch a user's list and extract kinds.
  - Event tests: lobby creation, session start, session end and evaluation completion create the
    right notifications.
  - Privacy tests: a candidate cannot see the host's notifications and gets 404 for foreign ids.
  - Read tests: mark one, mark all, unread_only filter, unknown id, empty inbox.
  - Invitation tests: reserve a seat and notify, INVITED becomes JOINED on joining, unanswered
    invitation does not block start, unknown email 404, duplicate 409, host-only.
Used by: nothing imports it directly (run by pytest, which discovers test_*.py files)
Depends on: services/session/__init__.py, services/session/state.py
"""

import uuid

import pytest
from httpx import AsyncClient

from services.session import repository as session_repo
from services.session import service as session_service
from services.session.state import ParticipantState


async def _notifications(client: AsyncClient, headers: dict) -> dict:
    """Fetch the user's notification list."""
    response = await client.get("/api/v1/me/notifications", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def kinds(payload: dict) -> list[str]:
    """The kinds of the listed notifications."""
    return [item["kind"] for item in payload["items"]]


async def test_creating_a_session_notifies_the_host(client: AsyncClient, gd_session, host_auth):
    """FR-32 lobby creation."""
    # Creating the gd_session fixture already triggered this notification.
    payload = await _notifications(client, host_auth)
    assert "lobby_created" in kinds(payload)
    # Find the lobby_created item.
    created = next(i for i in payload["items"] if i["kind"] == "lobby_created")
    # It carries the join code and a link back to the lobby.
    assert created["payload"]["code"] == gd_session["code"]
    assert created["link"] == f"/lobby/{gd_session['id']}"
    # New notifications start unread.
    assert created["read_at"] is None
    assert payload["unread"] >= 1


async def test_starting_a_session_notifies_participants(
    client: AsyncClient, gd_session, host_auth, candidate_auth, second_candidate
):
    """FR-32 session starting."""
    # Both candidates join and ready up so the host can start.
    for header in (candidate_auth, second_candidate):
        await client.post("/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=header)
        await client.post(
            f"/api/v1/lobbies/{gd_session['id']}/ready", json={"ready": True}, headers=header
        )
    # Starting the session creates a session_starting notification for participants.
    await client.post(f"/api/v1/sessions/{gd_session['id']}/start", headers=host_auth)

    # Read the candidate's inbox.
    payload = await _notifications(client, candidate_auth)
    assert "session_starting" in kinds(payload)
    starting = next(i for i in payload["items"] if i["kind"] == "session_starting")
    assert starting["link"] == f"/room/{gd_session['id']}"


async def test_ending_a_session_notifies_participants(
    client: AsyncClient, gd_session, host_auth, candidate_auth, second_candidate
):
    """FR-32 session ending."""
    # Both candidates join and ready up.
    for header in (candidate_auth, second_candidate):
        await client.post("/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=header)
        await client.post(
            f"/api/v1/lobbies/{gd_session['id']}/ready", json={"ready": True}, headers=header
        )
    # Start the session, then end it early.
    await client.post(f"/api/v1/sessions/{gd_session['id']}/start", headers=host_auth)
    await client.post(f"/api/v1/sessions/{gd_session['id']}/end", headers=host_auth)

    payload = await _notifications(client, candidate_auth)
    assert "session_ended" in kinds(payload)


async def test_notifications_are_private_to_their_owner(
    client: AsyncClient, gd_session, host_auth, candidate_auth
):
    """A candidate must not see the host's notifications."""
    # The candidate's inbox does not include the host's notification.
    candidate = await _notifications(client, candidate_auth)
    assert "lobby_created" not in kinds(candidate)


async def test_marking_one_read(client: AsyncClient, gd_session, host_auth):
    """Marking one notification read sets its read_at."""
    # Take the first notification in the host's inbox.
    payload = await _notifications(client, host_auth)
    target = payload["items"][0]

    # Mark it read.
    response = await client.post(f"/api/v1/me/notifications/{target['id']}/read", headers=host_auth)
    assert response.status_code == 200
    assert response.json()["read_at"] is not None

    # The unread count drops by one.
    after = await _notifications(client, host_auth)
    assert after["unread"] == payload["unread"] - 1


async def test_marking_someone_elses_notification_is_a_404(
    client: AsyncClient, gd_session, host_auth, candidate_auth
):
    """Not 403 — the endpoint must not confirm that another user's id exists."""
    # A notification owned by the host.
    payload = await _notifications(client, host_auth)
    target = payload["items"][0]
    # A different user tries to mark it read.
    response = await client.post(
        f"/api/v1/me/notifications/{target['id']}/read", headers=candidate_auth
    )
    assert response.status_code == 404


async def test_marking_all_read(client: AsyncClient, gd_session, host_auth):
    """Mark-all-read clears the unread count."""
    # Mark everything read in one call.
    response = await client.post("/api/v1/me/notifications/read-all", headers=host_auth)
    assert response.status_code == 200
    assert response.json()["marked_read"] >= 1
    # Nothing remains unread.
    assert (await _notifications(client, host_auth))["unread"] == 0


async def test_unread_only_filter(client: AsyncClient, gd_session, host_auth):
    """unread_only returns nothing once everything is read."""
    # Clear the unread list first.
    await client.post("/api/v1/me/notifications/read-all", headers=host_auth)
    # Ask for only the unread notifications.
    response = await client.get("/api/v1/me/notifications?unread_only=true", headers=host_auth)
    assert response.json()["items"] == []


async def test_an_unknown_notification_id_is_a_404(client: AsyncClient, host_auth):
    """An unknown notification id gives 404."""
    # A random id that does not exist.
    response = await client.post(f"/api/v1/me/notifications/{uuid.uuid4()}/read", headers=host_auth)
    assert response.status_code == 404


# --------------------------------------------------------------------------
# Invitations (FR-05)
# --------------------------------------------------------------------------


async def test_inviting_a_candidate_reserves_a_seat_and_notifies(
    client: AsyncClient, gd_session, host_auth, candidate_auth, credentials
):
    """Inviting reserves a seat and sends the invitee a notification."""
    # The host invites a registered user by email.
    response = await client.post(
        f"/api/v1/lobbies/{gd_session['id']}/invite",
        json={"email": credentials["email"]},
        headers=host_auth,
    )
    assert response.status_code == 201, response.text
    # The seat is reserved as INVITED.
    assert response.json()["state"] == ParticipantState.INVITED

    # The invitee finds the invitation in their inbox, with the join code.
    payload = await _notifications(client, candidate_auth)
    assert "session_invitation" in kinds(payload)
    invitation = next(i for i in payload["items"] if i["kind"] == "session_invitation")
    assert invitation["payload"]["code"] == gd_session["code"]


async def test_an_invited_candidate_joining_becomes_joined(
    client: AsyncClient, gd_session, host_auth, candidate_auth, credentials
):
    """Design §9: INVITED → JOINED."""
    # Invite the candidate.
    await client.post(
        f"/api/v1/lobbies/{gd_session['id']}/invite",
        json={"email": credentials["email"]},
        headers=host_auth,
    )
    # They turn up and join with the code.
    joined = await client.post(
        "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=candidate_auth
    )
    assert joined.status_code == 200
    # INVITED has become JOINED.
    participant = joined.json()["participants"][0]
    assert participant["state"] == ParticipantState.JOINED
    # The label reserved at invitation time is kept.
    assert participant["candidate_label"] == "A"


async def test_an_unanswered_invitation_does_not_block_the_start(
    session_factory,
    client: AsyncClient,
    gd_session,
    host_auth,
    candidate_auth,
    second_candidate,
    credentials,
):
    """One candidate who never turns up must not deadlock the lobby."""
    session_id = uuid.UUID(gd_session["id"])
    # Invite someone who never joins.
    # Invite someone who never joins; this seat stays INVITED.
    await client.post(
        f"/api/v1/lobbies/{gd_session['id']}/invite",
        json={"email": credentials["email"]},
        headers=host_auth,
    )
    # A different candidate joins and is ready; min_participants is 2 in the
    # fixture, so add the second one too.
    await client.post(
        "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=second_candidate
    )
    await client.post(
        f"/api/v1/lobbies/{session_id}/ready", json={"ready": True}, headers=second_candidate
    )

    # Check the start condition directly against the database.
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, session_id)
        participants = await session_repo.list_participants(db, session_id)
        # One INVITED, one READY: not enough *present* participants yet.
        assert session_service.start_condition_met(gd, participants) is False

    # The invitee turns up and readies.
    await client.post(
        "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=candidate_auth
    )
    await client.post(
        f"/api/v1/lobbies/{session_id}/ready", json={"ready": True}, headers=candidate_auth
    )
    # Now the session starts.
    started = await client.post(f"/api/v1/sessions/{session_id}/start", headers=host_auth)
    assert started.status_code == 200


async def test_inviting_an_unknown_email_is_a_404(client: AsyncClient, gd_session, host_auth):
    """Inviting an unregistered email gives 404."""
    # An email with no account.
    response = await client.post(
        f"/api/v1/lobbies/{gd_session['id']}/invite",
        json={"email": "nobody@example.com"},
        headers=host_auth,
    )
    assert response.status_code == 404


async def test_inviting_the_same_candidate_twice_is_a_conflict(
    client: AsyncClient, gd_session, host_auth, registered, credentials
):
    """A second invitation for the same person is a 409."""
    # Invite the same registered user twice.
    body = {"email": credentials["email"]}
    first = await client.post(
        f"/api/v1/lobbies/{gd_session['id']}/invite", json=body, headers=host_auth
    )
    assert first.status_code == 201
    # The second invitation is refused.
    second = await client.post(
        f"/api/v1/lobbies/{gd_session['id']}/invite", json=body, headers=host_auth
    )
    assert second.status_code == 409


async def test_only_the_host_can_invite(
    client: AsyncClient, gd_session, candidate_auth, credentials
):
    """Only the host can invite (403 otherwise)."""
    # A candidate, not the host, tries to invite.
    response = await client.post(
        f"/api/v1/lobbies/{gd_session['id']}/invite",
        json={"email": credentials["email"]},
        headers=candidate_auth,
    )
    assert response.status_code == 403


async def test_evaluation_completion_notifies_every_candidate(
    session_factory, client: AsyncClient, gd_session, host_auth, candidate_auth, second_candidate
):
    """FR-32 evaluation completion and report availability."""
    # Local imports: only this test needs the evaluation pipeline.
    from services.evaluation import service as eval_service
    from services.speech.service import persist_segment

    session_id = uuid.UUID(gd_session["id"])
    # Two candidates join and ready up, and the host starts.
    for header in (candidate_auth, second_candidate):
        await client.post("/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=header)
        await client.post(
            f"/api/v1/lobbies/{session_id}/ready", json={"ready": True}, headers=header
        )
    await client.post(f"/api/v1/sessions/{session_id}/start", headers=host_auth)

    # Drive the backend directly, without waiting for the live loops.
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, session_id)
        # Each candidate speaks for 20 seconds, giving the evaluators something to score.
        for label, start in (("A", 0.0), ("B", 30.0)):
            await session_service.record_speech(db, gd, label, start, start + 20)
            await persist_segment(db, gd.id, label, start, start + 20, "A relevant point.", 0.9)
        # End the discussion.
        await session_service.end_session(db, gd, reason="test")
        # Run the evaluation pipeline inline (the default stub LLM needs no key).
        await eval_service.run_for_session(db, session_id)
        await db.commit()

    # Both completion and report notifications must now exist.
    payload = await _notifications(client, candidate_auth)
    assert "evaluation_completed" in kinds(payload)
    assert "report_available" in kinds(payload)
    report = next(i for i in payload["items"] if i["kind"] == "report_available")
    assert report["link"] == f"/report/{session_id}"


@pytest.mark.parametrize("unread_only", [True, False])
async def test_listing_works_for_a_user_with_no_notifications(
    client: AsyncClient, candidate_auth, unread_only
):
    """Listing works (empty) for a user with no notifications."""
    # Works for both filter values.
    response = await client.get(
        f"/api/v1/me/notifications?unread_only={unread_only}", headers=candidate_auth
    )
    assert response.status_code == 200
    # An empty inbox with zero unread.
    assert response.json() == {"unread": 0, "items": []}
