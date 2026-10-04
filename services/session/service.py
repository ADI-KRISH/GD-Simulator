"""Session and lobby business rules (FR-03 – FR-07, FR-10, FR-11, design §7.5, §8, §9).

Every state change goes through the transition table in `state.py` and emits a
domain event. Nothing here calls the AI layer directly — session end publishes
`evaluation.requested` and returns (claude.md invariant 1).
"""

import logging
import secrets
import string
import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from services.auth.models import User
from services.common.errors import (
    ConflictError,
    InvalidStateTransition,
    NotFoundError,
    PermissionDeniedError,
)
from services.common.events import Event, publish
from services.common.timeutil import as_utc, utcnow
from services.notifications.models import NotificationKind
from services.notifications.service import notify
from services.session import repository as repo
from services.session.models import DEFAULT_WEIGHTS, GDSession, Participant, SpeechEvent
from services.session.schemas import (
    LobbyView,
    ParticipantOut,
    SessionCreate,
    SessionUpdate,
    TimerView,
    TopicView,
)
from services.session.state import (
    ParticipantState,
    SessionState,
    transition_participant,
    transition_session,
)

logger = logging.getLogger(__name__)

# Unambiguous alphabet — no 0/O or 1/I, because codes get read aloud (FR-05).
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 6
_LABELS = string.ascii_uppercase

# Phases reported by the timer (FR-10).
PHASE_PREPARATION = "preparation"
PHASE_DISCUSSION = "discussion"
PHASE_WAITING = "waiting"
PHASE_ENDED = "ended"

# "02:00 → Warning" in the FR-10 example.
WARNING_THRESHOLD_SECONDS = 120

# The host is not a candidate (FR-05: candidates join the lobby), but they still
# have to be able to watch and run the session they created. They get this
# identity instead of a candidate label — it is outside _LABELS, so it can never
# collide with one, and nothing in the transcript or the metrics is keyed to it.
OBSERVER_LABEL = "HOST"


async def _generate_code(session: AsyncSession) -> str:
    for _ in range(10):
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
        if not await repo.code_exists(session, code):
            return code
    raise ConflictError("could not allocate a unique session code")


def _next_label(used: set[str]) -> str:
    for letter in _LABELS:
        if letter not in used:
            return letter
    # 26 participants is far beyond max_participants, but fail loudly if hit.
    raise ConflictError("no candidate labels remain for this session")


async def _set_session_state(
    session: AsyncSession, gd: GDSession, target: SessionState
) -> GDSession:
    current = SessionState(gd.state)
    gd.state = str(transition_session(current, target))
    await session.flush()
    logger.info(
        "session state changed",
        extra={"session_id": str(gd.id), "from": str(current), "to": gd.state},
    )
    return gd


async def _set_participant_state(
    session: AsyncSession, participant: Participant, target: ParticipantState
) -> Participant:
    current = ParticipantState(participant.state)
    if current is target:
        return participant
    participant.state = str(transition_participant(current, target))
    await session.flush()
    return participant


# --------------------------------------------------------------------------
# FR-03 GD creation
# --------------------------------------------------------------------------


async def create_session(session: AsyncSession, host: User, payload: SessionCreate) -> GDSession:
    gd = GDSession(
        code=await _generate_code(session),
        host_id=host.id,
        topic=payload.topic.strip(),
        topic_category=payload.topic_category,
        gd_type=str(payload.gd_type),
        difficulty=str(payload.difficulty),
        min_participants=payload.min_participants,
        max_participants=payload.max_participants,
        duration_seconds=payload.duration_seconds,
        preparation_seconds=payload.preparation_seconds,
        video_required=payload.video_required,
        recording_enabled=payload.recording_enabled,
        rubric_weights=payload.rubric_weights or dict(DEFAULT_WEIGHTS),
        state=str(SessionState.CREATED),
    )
    created = await repo.create_session(session, gd)
    # The lobby opens the moment the session exists (FR-04).
    await _set_session_state(session, created, SessionState.WAITING)
    await notify(
        session,
        [host.id],
        NotificationKind.LOBBY_CREATED,
        "Your discussion is ready",
        f"Share the code {created.code} with candidates so they can join.",
        session_id=created.id,
        link=f"/lobby/{created.id}",
        payload={"code": created.code},
    )
    logger.info("session created", extra={"session_id": str(created.id), "code": created.code})
    return created


async def update_session(session: AsyncSession, gd: GDSession, payload: SessionUpdate) -> GDSession:
    if SessionState(gd.state) not in {SessionState.WAITING, SessionState.READY}:
        raise ConflictError("a session can only be edited before it starts")
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(gd, field, str(value) if field in {"gd_type", "difficulty"} else value)
    await session.flush()
    return gd


async def require_session(session: AsyncSession, session_id: uuid.UUID) -> GDSession:
    gd = await repo.get_session_by_id(session, session_id)
    if gd is None:
        raise NotFoundError("session not found")
    return gd


def require_host(gd: GDSession, user: User) -> None:
    if gd.host_id != user.id and user.role != "admin":
        raise PermissionDeniedError("only the host can perform this action")


async def require_participant(
    session: AsyncSession, session_id: uuid.UUID, user: User
) -> Participant:
    participant = await repo.get_participant(session, session_id, user.id)
    if participant is None:
        raise PermissionDeniedError("you are not a participant in this session")
    return participant


async def require_member(session: AsyncSession, gd: GDSession, user: User) -> Participant | None:
    """A participant, or None for the host observing their own session.

    Everything past the lobby — the control socket, the media token, the timer,
    the topic — used to require a participant row, which the host never has. So
    a host could create a session and then not get into it at all.
    """
    if gd.host_id == user.id:
        participant = await repo.get_participant(session, gd.id, user.id)
        if participant is None or ParticipantState(participant.state) is ParticipantState.LEFT:
            return None
        return participant
    return await require_participant(session, gd.id, user)


# --------------------------------------------------------------------------
# FR-05 / FR-06 / FR-07 lobby membership
# --------------------------------------------------------------------------


async def join_by_code(
    session: AsyncSession, user: User, code: str
) -> tuple[GDSession, Participant]:
    gd = await repo.get_session_by_code(session, code)
    if gd is None:
        raise NotFoundError("no session with that code")

    existing = await repo.get_participant(session, gd.id, user.id)
    if existing is not None:
        if ParticipantState(existing.state) is ParticipantState.LEFT:
            raise ConflictError("you have already left this session")
        # An invited candidate turning up converts INVITED -> JOINED (design §9).
        if ParticipantState(existing.state) is ParticipantState.INVITED:
            await _set_participant_state(session, existing, ParticipantState.JOINED)
            await _refresh_readiness(session, gd)
        # Otherwise idempotent: refreshing the lobby page must not fail.
        return gd, existing

    state = SessionState(gd.state)
    if state not in {SessionState.WAITING, SessionState.READY}:
        raise ConflictError(f"this session is not accepting participants (state {state})")

    current = await repo.list_active_participants(session, gd.id)
    if len(current) >= gd.max_participants:
        raise ConflictError("this session is full")

    participant = Participant(
        session_id=gd.id,
        user_id=user.id,
        candidate_label=_next_label(await repo.used_labels(session, gd.id)),
        display_name=user.full_name,
        state=str(ParticipantState.JOINED),
    )
    await repo.add_participant(session, participant)
    await _refresh_readiness(session, gd)
    await publish(
        Event.PARTICIPANT_JOINED,
        str(gd.id),
        {
            "participant_id": str(participant.id),
            "candidate_label": participant.candidate_label,
            "display_name": participant.display_name,
        },
    )
    return gd, participant


async def invite(session: AsyncSession, gd: GDSession, email: str) -> Participant:
    """FR-05 (invitation) + FR-32 (session invitation).

    Reserves a seat and a candidate label for a registered user, and notifies
    them. The invitee still joins through the normal path — the invitation is a
    reservation plus a pointer, not a separate way in, so there is only one
    join code path to keep correct.
    """
    from services.auth import repository as auth_repo

    invitee = await auth_repo.get_user_by_email(session, email)
    if invitee is None:
        raise NotFoundError("no account with that email")

    if SessionState(gd.state) not in {SessionState.WAITING, SessionState.READY}:
        raise ConflictError("invitations can only be sent before the session starts")

    existing = await repo.get_participant(session, gd.id, invitee.id)
    if existing is not None:
        raise ConflictError("that candidate is already in this session")

    if len(await repo.list_active_participants(session, gd.id)) >= gd.max_participants:
        raise ConflictError("this session is full")

    participant = Participant(
        session_id=gd.id,
        user_id=invitee.id,
        candidate_label=_next_label(await repo.used_labels(session, gd.id)),
        display_name=invitee.full_name,
        state=str(ParticipantState.INVITED),
    )
    await repo.add_participant(session, participant)
    await notify(
        session,
        [invitee.id],
        NotificationKind.SESSION_INVITATION,
        "You have been invited to a group discussion",
        f"{gd.topic} — join with code {gd.code}.",
        session_id=gd.id,
        link=f"/lobby/{gd.id}",
        payload={"code": gd.code},
    )
    return participant


async def leave(session: AsyncSession, gd: GDSession, user: User) -> None:
    participant = await require_participant(session, gd.id, user)
    if ParticipantState(participant.state) is ParticipantState.LEFT:
        return
    await _set_participant_state(session, participant, ParticipantState.LEFT)
    participant.is_ready = False
    participant.left_at = utcnow()
    await session.flush()
    await _refresh_readiness(session, gd)
    await publish(
        Event.PARTICIPANT_LEFT, str(gd.id), {"candidate_label": participant.candidate_label}
    )


async def set_ready(session: AsyncSession, gd: GDSession, user: User, ready: bool) -> Participant:
    if SessionState(gd.state) not in {SessionState.WAITING, SessionState.READY}:
        raise ConflictError("readiness can only change before the session starts")
    participant = await require_participant(session, gd.id, user)
    await _set_participant_state(
        session,
        participant,
        ParticipantState.READY if ready else ParticipantState.JOINED,
    )
    participant.is_ready = ready
    await session.flush()
    await _refresh_readiness(session, gd)
    await publish(
        Event.PARTICIPANT_READY,
        str(gd.id),
        {"candidate_label": participant.candidate_label, "ready": ready},
    )
    return participant


def start_condition_met(gd: GDSession, participants: list[Participant]) -> bool:
    """FR-07: enough participants present, and all of them ready.

    An INVITED candidate has been offered a seat but has not turned up, so they
    are not counted and their readiness is not required — otherwise one
    unanswered invitation would deadlock the lobby.
    """
    present = [
        p
        for p in participants
        if ParticipantState(p.state) not in {ParticipantState.LEFT, ParticipantState.INVITED}
    ]
    if len(present) < gd.min_participants:
        return False
    return all(p.is_ready for p in present)


async def _refresh_readiness(session: AsyncSession, gd: GDSession) -> None:
    """Keep the session's WAITING ⇄ READY state in step with the lobby."""
    participants = await repo.list_participants(session, gd.id)
    met = start_condition_met(gd, participants)
    current = SessionState(gd.state)
    if met and current is SessionState.WAITING:
        await _set_session_state(session, gd, SessionState.READY)
    elif not met and current is SessionState.READY:
        await _set_session_state(session, gd, SessionState.WAITING)


async def lobby_view(session: AsyncSession, gd: GDSession) -> LobbyView:
    """FR-04."""
    participants = await repo.list_participants(session, gd.id)
    active = [p for p in participants if ParticipantState(p.state) is not ParticipantState.LEFT]
    return LobbyView(
        # The lobby is 1:1 with its session, so the session id identifies it.
        lobby_id=gd.id,
        session_id=gd.id,
        code=gd.code,
        topic=gd.topic,
        session_state=SessionState(gd.state),
        capacity=gd.max_participants,
        min_participants=gd.min_participants,
        occupancy=len(active),
        ready_count=sum(1 for p in active if p.is_ready),
        start_condition_met=start_condition_met(gd, participants),
        participants=[ParticipantOut.model_validate(p) for p in active],
    )


# --------------------------------------------------------------------------
# Session lifecycle (design §8, §18)
# --------------------------------------------------------------------------


async def start_session(session: AsyncSession, gd: GDSession) -> GDSession:
    participants = await repo.list_participants(session, gd.id)
    if not start_condition_met(gd, participants):
        raise ConflictError(
            f"start condition not met: need {gd.min_participants} ready participants"
        )
    await _set_session_state(session, gd, SessionState.STARTING)
    gd.preparation_started_at = utcnow()
    await session.flush()
    await publish(
        Event.SESSION_STARTED,
        str(gd.id),
        {"phase": PHASE_PREPARATION, "preparation_seconds": gd.preparation_seconds},
    )
    participants_now = await repo.list_active_participants(session, gd.id)
    await notify(
        session,
        [p.user_id for p in participants_now],
        NotificationKind.SESSION_STARTING,
        "Your discussion is starting",
        gd.topic,
        session_id=gd.id,
        link=f"/room/{gd.id}",
    )
    # A zero-length preparation goes straight to the discussion.
    if gd.preparation_seconds == 0:
        await go_live(session, gd)
    return gd


async def go_live(session: AsyncSession, gd: GDSession) -> GDSession:
    """STARTING → LIVE. The discussion clock starts here (t=0 for transcripts)."""
    await _set_session_state(session, gd, SessionState.LIVE)
    gd.started_at = utcnow()
    await session.flush()
    for participant in await repo.list_active_participants(session, gd.id):
        if ParticipantState(participant.state) is ParticipantState.READY:
            await _set_participant_state(session, participant, ParticipantState.ACTIVE)
    await publish(
        Event.SESSION_STARTED,
        str(gd.id),
        {"phase": PHASE_DISCUSSION, "duration_seconds": gd.duration_seconds},
    )
    return gd


async def end_session(
    session: AsyncSession, gd: GDSession, *, reason: str = "timer_elapsed"
) -> GDSession:
    """LIVE → ENDING → ENDED (design §18).

    Evaluation is requested downstream by publishing an event. This function
    must never block on, or fail because of, the AI layer.
    """
    if SessionState(gd.state) in {SessionState.ENDING, SessionState.ENDED}:
        return gd
    await _set_session_state(session, gd, SessionState.ENDING)
    await _set_session_state(session, gd, SessionState.ENDED)
    gd.ended_at = utcnow()
    await session.flush()
    await publish(Event.SESSION_ENDED, str(gd.id), {"reason": reason})
    await notify(
        session,
        [p.user_id for p in await repo.list_active_participants(session, gd.id)],
        NotificationKind.SESSION_ENDED,
        "Discussion ended",
        "Your evaluation is being prepared. It runs after the discussion, so nothing "
        "you did depended on it.",
        session_id=gd.id,
        link=f"/report/{gd.id}",
    )
    logger.info("session ended", extra={"session_id": str(gd.id), "reason": reason})
    return gd


async def fail_session(session: AsyncSession, gd: GDSession, reason: str) -> GDSession:
    gd.failure_reason = reason
    await _set_session_state(session, gd, SessionState.FAILED)
    return gd


# --------------------------------------------------------------------------
# Connection lifecycle — a disconnect must never end the session (design §9)
# --------------------------------------------------------------------------


async def mark_disconnected(session: AsyncSession, participant: Participant) -> Participant:
    if ParticipantState(participant.state) is ParticipantState.LEFT:
        return participant
    await _set_participant_state(session, participant, ParticipantState.DISCONNECTED)
    participant.connection_drops += 1
    participant.last_seen_at = utcnow()
    await session.flush()
    await publish(
        Event.PARTICIPANT_DISCONNECTED,
        str(participant.session_id),
        {"candidate_label": participant.candidate_label},
    )
    return participant


async def mark_reconnected(session: AsyncSession, participant: Participant) -> Participant:
    if ParticipantState(participant.state) is ParticipantState.DISCONNECTED:
        await _set_participant_state(session, participant, ParticipantState.RECONNECTED)
        await _set_participant_state(session, participant, ParticipantState.ACTIVE)
        await publish(
            Event.PARTICIPANT_RECONNECTED,
            str(participant.session_id),
            {"candidate_label": participant.candidate_label},
        )
    participant.last_seen_at = utcnow()
    await session.flush()
    return participant


# --------------------------------------------------------------------------
# FR-10 timer / FR-11 topic delivery
# --------------------------------------------------------------------------


def _elapsed(since: datetime | None, now: datetime | None = None) -> float:
    if since is None:
        return 0.0
    return ((now or utcnow()) - as_utc(since)).total_seconds()


def timer_view(gd: GDSession, now: datetime | None = None) -> TimerView:
    """Server-authoritative clock.

    Derived from stored timestamps rather than counted down in memory, so it
    survives a restart and every client agrees on the same number.
    """
    state = SessionState(gd.state)
    if state is SessionState.STARTING:
        remaining = gd.preparation_seconds - _elapsed(gd.preparation_started_at, now)
        phase, duration = PHASE_PREPARATION, gd.preparation_seconds
    elif state is SessionState.LIVE:
        remaining = gd.duration_seconds - _elapsed(gd.started_at, now)
        phase, duration = PHASE_DISCUSSION, gd.duration_seconds
    elif state in {SessionState.WAITING, SessionState.READY, SessionState.CREATED}:
        phase, duration, remaining = PHASE_WAITING, gd.preparation_seconds, gd.preparation_seconds
    else:
        phase, duration, remaining = PHASE_ENDED, gd.duration_seconds, 0

    remaining = max(0, int(remaining))
    return TimerView(
        session_id=gd.id,
        phase=phase,
        seconds_remaining=remaining,
        duration_seconds=duration,
        warning=phase == PHASE_DISCUSSION and 0 < remaining <= WARNING_THRESHOLD_SECONDS,
    )


def elapsed_discussion_seconds(gd: GDSession, now: datetime | None = None) -> float:
    """Seconds since the discussion went LIVE — the clock every transcript
    segment, speech event and metric is measured against."""
    if gd.started_at is None:
        return 0.0
    return max(0.0, _elapsed(gd.started_at, now))


def topic_view(gd: GDSession) -> TopicView:
    """FR-11.

    The topic is withheld until preparation begins, so nobody can prepare ahead
    of the others.
    """
    released = SessionState(gd.state) not in {
        SessionState.CREATED,
        SessionState.WAITING,
        SessionState.READY,
    }
    return TopicView(
        session_id=gd.id,
        topic=gd.topic if released else "",
        topic_category=gd.topic_category,
        difficulty=gd.difficulty,
        released=released,
    )


async def advance_clock(session: AsyncSession, gd: GDSession) -> GDSession:
    """Apply whatever the clock says should already have happened.

    Called by the timer ticker and lazily on API reads, so the lifecycle stays
    correct even when no ticker is running (design §10, §18).
    """
    state = SessionState(gd.state)
    if state is SessionState.STARTING:
        if _elapsed(gd.preparation_started_at) >= gd.preparation_seconds:
            await go_live(session, gd)
    elif state is SessionState.LIVE:
        if _elapsed(gd.started_at) >= gd.duration_seconds:
            await end_session(session, gd, reason="timer_elapsed")
    return gd


# --------------------------------------------------------------------------
# Speech activity (FR-09) — feeds the deterministic metrics engine
# --------------------------------------------------------------------------


async def record_speech(
    session: AsyncSession,
    gd: GDSession,
    candidate_label: str,
    start: float,
    end: float,
) -> SpeechEvent:
    if end < start:
        raise InvalidStateTransition("speech event ends before it starts")
    event = SpeechEvent(session_id=gd.id, candidate_label=candidate_label, start=start, end=end)
    await repo.add_speech_event(session, event)
    await publish(
        Event.SPEECH_ENDED,
        str(gd.id),
        {"candidate_label": candidate_label, "start": start, "end": end},
    )
    return event
