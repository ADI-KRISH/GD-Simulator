"""Session lifecycle at the service layer (design §8, §9, §10, §18).

These drive `services.session.service` directly rather than through HTTP,
because the behaviour under test — the timer advancing, a candidate dropping
and coming back — is about state over time, not about request handling.
"""

import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient

from services.common.errors import ConflictError
from services.common.timeutil import utcnow
from services.session import repository as session_repo
from services.session import service as session_service
from services.session.state import ParticipantState, SessionState


@pytest.fixture
async def ready_session(
    session_factory, client: AsyncClient, gd_session, host_auth, candidate_auth, second_candidate
):
    """Two joined, ready candidates. Not started."""
    session_id = uuid.UUID(gd_session["id"])
    for header in (candidate_auth, second_candidate):
        await client.post("/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=header)
        await client.post(
            f"/api/v1/lobbies/{session_id}/ready", json={"ready": True}, headers=header
        )
    return session_id


# --------------------------------------------------------------------------
# Disconnect / reconnect (design §9, §38)
# --------------------------------------------------------------------------


async def test_a_disconnect_does_not_end_the_session(session_factory, ready_session, events):
    """The central FR-38 requirement: the session continues."""
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, ready_session)
        await session_service.start_session(db, gd)
        participant = await session_repo.get_participant_by_label(db, gd.id, "A")

        await session_service.mark_disconnected(db, participant)
        await db.commit()

        assert ParticipantState(participant.state) is ParticipantState.DISCONNECTED
        assert participant.connection_drops == 1
        # The session is untouched.
        assert SessionState(gd.state) is SessionState.LIVE

    assert events.count("participant.disconnected") == 1


async def test_reconnecting_returns_the_participant_to_active(
    session_factory, ready_session, events
):
    """DISCONNECTED → RECONNECTED → ACTIVE (design §9)."""
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, ready_session)
        await session_service.start_session(db, gd)
        participant = await session_repo.get_participant_by_label(db, gd.id, "A")

        await session_service.mark_disconnected(db, participant)
        await session_service.mark_reconnected(db, participant)
        await db.commit()

    assert ParticipantState(participant.state) is ParticipantState.ACTIVE
    assert events.count("participant.reconnected") == 1


async def test_repeated_drops_are_counted(session_factory, ready_session):
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, ready_session)
        await session_service.start_session(db, gd)
        participant = await session_repo.get_participant_by_label(db, gd.id, "A")

        for _ in range(3):
            await session_service.mark_disconnected(db, participant)
            await session_service.mark_reconnected(db, participant)
        await db.commit()

    assert participant.connection_drops == 3
    assert ParticipantState(participant.state) is ParticipantState.ACTIVE


async def test_a_candidate_who_left_is_not_marked_disconnected(
    session_factory, ready_session, candidate_auth, client
):
    """Leaving is terminal; a socket closing afterwards must not revive them."""
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, ready_session)
        participant = await session_repo.get_participant_by_label(db, gd.id, "A")
        participant.state = str(ParticipantState.LEFT)
        await db.flush()

        await session_service.mark_disconnected(db, participant)
        await db.commit()

    assert ParticipantState(participant.state) is ParticipantState.LEFT


# --------------------------------------------------------------------------
# Timer and lifecycle (FR-10, design §18)
# --------------------------------------------------------------------------


async def test_preparation_runs_before_the_discussion(
    session_factory, client, host_auth, session_payload, candidate_auth, second_candidate
):
    """FR-10: prep timer, then the GD timer."""
    payload = session_payload | {"preparation_seconds": 120}
    created = await client.post("/api/v1/sessions", json=payload, headers=host_auth)
    session = created.json()
    for header in (candidate_auth, second_candidate):
        await client.post("/api/v1/lobbies/join", json={"code": session["code"]}, headers=header)
        await client.post(
            f"/api/v1/lobbies/{session['id']}/ready", json={"ready": True}, headers=header
        )

    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, uuid.UUID(session["id"]))
        await session_service.start_session(db, gd)
        await db.commit()

        assert SessionState(gd.state) is SessionState.STARTING
        timer = session_service.timer_view(gd)
        assert timer.phase == "preparation"
        assert 0 < timer.seconds_remaining <= 120
        # FR-11: the topic is released the moment preparation begins.
        assert session_service.topic_view(gd).released is True


async def test_the_clock_promotes_starting_to_live(session_factory, ready_session):
    """`advance_clock` applies what the clock says should have happened."""
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, ready_session)
        gd.preparation_seconds = 120
        await db.flush()
        await session_service.start_session(db, gd)

        # Rewind the start so preparation has notionally elapsed.
        gd.preparation_started_at = utcnow() - timedelta(seconds=121)
        await db.flush()
        await session_service.advance_clock(db, gd)
        await db.commit()

    assert SessionState(gd.state) is SessionState.LIVE
    assert gd.started_at is not None


async def test_the_clock_ends_the_session_when_time_runs_out(
    session_factory, ready_session, events
):
    """Design §18: LIVE → ENDING → ENDED when the timer reaches zero."""
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, ready_session)
        await session_service.start_session(db, gd)
        gd.started_at = utcnow() - timedelta(seconds=gd.duration_seconds + 1)
        await db.flush()
        await session_service.advance_clock(db, gd)
        await db.commit()

    assert SessionState(gd.state) is SessionState.ENDED
    assert gd.ended_at is not None
    assert events.count("session.ended") == 1


async def test_the_timer_warns_near_the_end(session_factory, ready_session):
    """FR-10: "02:00 → Warning"."""
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, ready_session)
        await session_service.start_session(db, gd)
        gd.started_at = utcnow() - timedelta(seconds=gd.duration_seconds - 90)
        await db.flush()
        timer = session_service.timer_view(gd)

    assert timer.phase == "discussion"
    assert timer.warning is True
    assert timer.seconds_remaining <= 120


async def test_the_timer_is_derived_not_counted(session_factory, ready_session):
    """Reading twice a moment apart must give a smaller number without any
    tick having run — the clock survives a restart."""
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, ready_session)
        await session_service.start_session(db, gd)
        now = utcnow()
        first = session_service.timer_view(gd, now=now)
        later = session_service.timer_view(gd, now=now + timedelta(seconds=30))

    assert first.seconds_remaining - later.seconds_remaining == 30


async def test_ending_twice_is_idempotent(session_factory, ready_session, events):
    """A host clicking End while the timer fires must not produce two endings."""
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, ready_session)
        await session_service.start_session(db, gd)
        await session_service.end_session(db, gd, reason="host_ended")
        await session_service.end_session(db, gd, reason="timer_elapsed")
        await db.commit()

    assert SessionState(gd.state) is SessionState.ENDED
    assert events.count("session.ended") == 1


async def test_a_session_cannot_start_twice(session_factory, ready_session):
    from services.common.errors import InvalidStateTransition

    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, ready_session)
        await session_service.start_session(db, gd)
        with pytest.raises(InvalidStateTransition):
            await session_service.start_session(db, gd)


async def test_readiness_cannot_change_once_live(
    session_factory, ready_session, client, candidate_auth
):
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, ready_session)
        await session_service.start_session(db, gd)
        await db.commit()

    response = await client.post(
        f"/api/v1/lobbies/{ready_session}/ready", json={"ready": False}, headers=candidate_auth
    )
    assert response.status_code == 409


async def test_speech_events_are_recorded_on_the_session_clock(session_factory, ready_session):
    """These rows are what the deterministic metrics engine reads."""
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, ready_session)
        await session_service.start_session(db, gd)
        await session_service.record_speech(db, gd, "A", 10.0, 25.0)
        await session_service.record_speech(db, gd, "B", 26.0, 30.0)
        await db.commit()
        events_rows = await session_repo.list_speech_events(db, gd.id)

    assert [(e.candidate_label, e.start, e.end) for e in events_rows] == [
        ("A", 10.0, 25.0),
        ("B", 26.0, 30.0),
    ]


async def test_a_backwards_speech_event_is_rejected(session_factory, ready_session):
    from services.common.errors import InvalidStateTransition

    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, ready_session)
        await session_service.start_session(db, gd)
        with pytest.raises(InvalidStateTransition):
            await session_service.record_speech(db, gd, "A", 30.0, 10.0)


async def test_evaluation_cannot_be_requested_for_a_live_session(session_factory, ready_session):
    from services.evaluation import service as eval_service

    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, ready_session)
        await session_service.start_session(db, gd)
        with pytest.raises(ConflictError):
            await eval_service.request_evaluation(db, gd)
