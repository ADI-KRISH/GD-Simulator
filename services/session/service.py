"""Session and lobby business rules (FR-03 – FR-07, FR-10, FR-11, design §7.5, §8, §9).

Every state change goes through the transition table in `state.py` and emits a
domain event. Nothing here calls the AI layer directly — session end publishes
`evaluation.requested` and returns (claude.md invariant 1).

Purpose: The business rules for group discussions: create and edit a session, run the lobby
    (join, invite, ready, leave), drive the lifecycle (start, go live, end, fail), serve the
    server-authoritative timer and topic, and record speech and turns.
Why needed: It is the single place that changes session and participant state, always through
    the transition tables in state.py and always emitting a domain event. Routes, websockets and
    the ticker call it instead of touching rows directly, so rules such as one seat per student
    or no edits after start cannot be bypassed.
Contains:
  - _generate_code / _next_label: unpredictable join codes and the next free candidate label (A,
    B, ...).
  - _set_session_state / _set_participant_state: the only way states change, validated by the
    transition tables.
  - create_session / update_session: build a lobby (FR-03) and let the host edit it before it
    starts.
  - require_session / require_host / require_participant / require_member: load-and-authorise
    helpers shared by every route.
  - join_by_code / invite / leave / set_ready: lobby membership (FR-05 to FR-07).
  - start_condition_met / _refresh_readiness / lobby_view: keep WAITING and READY in step with
    who is ready, and build the lobby snapshot.
  - start_session / go_live / end_session / fail_session: lifecycle transitions; ending only
    publishes an event so the AI layer stays downstream.
  - mark_disconnected / mark_reconnected: a dropped connection never ends the session (design
    §9).
  - timer_view / elapsed_discussion_seconds / topic_view / advance_clock: server-authoritative
    clock and delayed topic release (FR-10, FR-11).
  - record_speech / record_speech_tracked / interruption_times: store raw speech and work out
    interruptions and replies for the metrics engine.
  - list_open_lobbies: public lobby browser.
Used by: services/lobby/router.py, services/session/router.py,
    services/tests/test_host_in_session.py, services/tests/test_realtime_ws.py
Depends on: services/auth/models.py, services/common/errors.py, services/common/events.py,
    services/common/timeutil.py, services/metrics/engine.py, services/notifications/models.py,
    services/notifications/service.py, services/session/__init__.py, services/session/models.py,
    services/session/schemas.py, services/session/state.py, services/speech/diarization.py
"""

import logging  # state-change and lifecycle logs
import secrets  # unpredictable join codes
import string  # uppercase letters for candidate labels
import uuid  # session/user id types
from datetime import datetime  # clock arithmetic for phases

from sqlalchemy.ext.asyncio import AsyncSession  # database session

from services.auth.models import User  # the acting user
from services.common.errors import (  # 409 / state / 404 / 403 errors
    ConflictError,
    InvalidStateTransition,
    NotFoundError,
    PermissionDeniedError,
)
from services.common.events import Event, publish  # domain events (never raise)
from services.common.timeutil import as_utc, utcnow  # UTC-safe time helpers
from services.metrics.engine import RESPONSE_WINDOW_SECONDS  # max gap that counts as a reply
from services.notifications.models import NotificationKind  # notification types
from services.notifications.service import notify  # create in-app notifications
from services.session import repository as repo  # data access
from services.session.models import DEFAULT_WEIGHTS, GDSession, Participant, SpeechEvent, Turn
from services.session.schemas import (  # view/request shapes
    LobbyListItem,
    LobbyView,
    ParticipantOut,
    SessionCreate,
    SessionUpdate,
    TimerView,
    TopicView,
)
from services.session.state import (  # state enums and validated transitions
    ParticipantState,
    SessionState,
    transition_participant,
    transition_session,
)
from services.speech.diarization import MIN_OVERLAP_SECONDS  # overlap threshold for interruptions

logger = logging.getLogger(__name__)

# Unambiguous alphabet — no 0/O or 1/I, because codes get read aloud (FR-05).
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 6
# Candidate labels A to Z; the host observer label HOST is deliberately not part of this set.
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
    """Make a random join code that no other session uses (up to 10 attempts)."""
    # Retry on collision; with this alphabet and length a clash is rare.
    for _ in range(10):
        # Use secrets (not random) so codes cannot be predicted.
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
        if not await repo.code_exists(session, code):
            return code
    # Ten clashes in a row means something is wrong; fail instead of looping forever.
    raise ConflictError("could not allocate a unique session code")


def _next_label(used: set[str]) -> str:
    """Pick the first unused candidate label (A, B, C, ...)."""
    # Labels are handed out in order, so the first free letter is next.
    for letter in _LABELS:
        if letter not in used:
            return letter
    # 26 participants is far beyond max_participants, but fail loudly if hit.
    raise ConflictError("no candidate labels remain for this session")


async def _set_session_state(
    session: AsyncSession, gd: GDSession, target: SessionState
) -> GDSession:
    """Move a session to `target` through the transition table and log it."""
    # gd.state is stored as text; convert it to the enum for the transition table.
    current = SessionState(gd.state)
    # Validate the move (raises InvalidStateTransition if illegal) before changing the row.
    gd.state = str(transition_session(current, target))
    await session.flush()
    # Log every lifecycle change so a session's history can be rebuilt from the logs.
    logger.info(
        "session state changed",
        extra={"session_id": str(gd.id), "from": str(current), "to": gd.state},
    )
    return gd


async def _set_participant_state(
    session: AsyncSession, participant: Participant, target: ParticipantState
) -> Participant:
    """Move a participant to `target`; a no-op if they are already there."""
    current = ParticipantState(participant.state)
    # Already there: nothing to do, which keeps repeated calls harmless.
    if current is target:
        return participant
    # Validate against the participant table, then persist.
    participant.state = str(transition_participant(current, target))
    await session.flush()
    return participant


# --------------------------------------------------------------------------
# FR-03 GD creation
# --------------------------------------------------------------------------


async def assert_not_in_active_session(
    session: AsyncSession, user: User, *, except_session: uuid.UUID | None = None
) -> None:
    """One live seat per student: you cannot sit in two unfinished discussions."""
    # Find any unfinished session this user is already seated in.
    current = await repo.active_session_for_user(session, user.id)
    # Allow the one session we are explicitly acting on (except_session).
    if current is not None and current.id != except_session:
        raise ConflictError(
            f"you are already in a discussion ({current.code}); leave or finish it first"
        )


async def create_session(session: AsyncSession, host: User, payload: SessionCreate) -> GDSession:
    """Create a session in the lobby (WAITING) state and notify the creator."""
    # A student who creates a lobby sits in it, so they are bound by the same
    # one-seat rule as anyone joining. Hosts/admins observe and are not seated.
    seated = host.role == "candidate" or payload.mode == "solo"
    if seated:
        await assert_not_in_active_session(session, host)
    # Copy the validated settings onto the row; fall back to the default rubric if none was given.
    gd = GDSession(
        mode=payload.mode,
        personas=payload.personas,
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
    # Insert the row (state CREATED).
    created = await repo.create_session(session, gd)
    # The lobby opens the moment the session exists (FR-04).
    await _set_session_state(session, created, SessionState.WAITING)
    # Seat the creator as candidate A so a student can take part in their own lobby.
    if seated:
        await repo.add_participant(
            session,
            Participant(
                session_id=created.id,
                user_id=host.id,
                candidate_label="A",
                display_name=host.full_name,
                state=str(ParticipantState.JOINED),
            ),
        )
        # Keep WAITING and READY in sync now that a participant exists.
        await _refresh_readiness(session, created)
        # Reload the participants relationship so the response includes the creator's seat.
        await session.refresh(created, ["participants"])
    # Tell the creator the lobby is open and how to share the code.
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
    """Apply host edits; only allowed while the session is still in the lobby."""
    # Edits are only legal while the lobby is open (WAITING or READY).
    if SessionState(gd.state) not in {SessionState.WAITING, SessionState.READY}:
        raise ConflictError("a session can only be edited before it starts")
    # Apply only the fields the host sent; enums are stored as plain strings.
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(gd, field, str(value) if field in {"gd_type", "difficulty"} else value)
    await session.flush()
    return gd


async def require_session(session: AsyncSession, session_id: uuid.UUID) -> GDSession:
    """Load a session or raise 404."""
    gd =await repo.get_session_by_id(session, session_id)
    if gd is None:
        raise NotFoundError("session not found")
    return gd


def require_host(gd: GDSession, user: User) -> None:
    """Raise 403 unless the user is the session's host (or an admin)."""
    # Admins may act on any session; everyone else must be the host.
    if gd.host_id != user.id and user.role != "admin":
        raise PermissionDeniedError("only the host can perform this action")


async def require_participant(
    session: AsyncSession, session_id: uuid.UUID, user: User
) -> Participant:
    """Return the user's seat in the session or raise 403."""
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
    # The host may not have a seat; return None so callers treat them as an observer.
    if gd.host_id == user.id:
        participant = await repo.get_participant(session, gd.id, user.id)
        # No seat, or they left: they can only watch.
        if participant is None or ParticipantState(participant.state) is ParticipantState.LEFT:
            return None
        return participant
    # Everyone else must hold a seat.
    return await require_participant(session, gd.id, user)


# --------------------------------------------------------------------------
# FR-05 / FR-06 / FR-07 lobby membership
# --------------------------------------------------------------------------


async def join_by_code(
    session: AsyncSession, user: User, code: str
) -> tuple[GDSession, Participant]:
    """Join a lobby by code (FR-05); idempotent for someone already seated."""
    # The code is the only way into a lobby (FR-05).
    gd = await repo.get_session_by_code(session, code)
    if gd is None:
        raise NotFoundError("no session with that code")

    # Re-joining is allowed and idempotent, so check for an existing seat first.
    existing = await repo.get_participant(session, gd.id, user.id)
    if existing is not None:
        # Someone who left cannot come back into the same session.
        if ParticipantState(existing.state) is ParticipantState.LEFT:
            raise ConflictError("you have already left this session")
        # An invited candidate turning up converts INVITED -> JOINED (design §9).
        if ParticipantState(existing.state) is ParticipantState.INVITED:
            await _set_participant_state(session, existing, ParticipantState.JOINED)
            await _refresh_readiness(session, gd)
        # Otherwise idempotent: refreshing the lobby page must not fail.
        return gd, existing

    # New people can only enter while the lobby is open.
    state = SessionState(gd.state)
    if state not in {SessionState.WAITING, SessionState.READY}:
        raise ConflictError(f"this session is not accepting participants (state {state})")
    # Solo practice rooms are private and cannot be joined by others.
    if gd.mode == "solo":
        raise ConflictError("this is a private practice session")
    # One unfinished discussion at a time per student.
    await assert_not_in_active_session(session, user)

    # Count everyone who has not left (disconnected people still hold a seat).
    current = await repo.list_active_participants(session, gd.id)
    if len(current) >= gd.max_participants:
        raise ConflictError("this session is full")

    # Create the seat with the next free label; the display name is copied from the account at join
    # time.
    participant = Participant(
        session_id=gd.id,
        user_id=user.id,
        candidate_label=_next_label(await repo.used_labels(session, gd.id)),
        display_name=user.full_name,
        state=str(ParticipantState.JOINED),
    )
    await repo.add_participant(session, participant)
    # A new person can change whether the start condition is met.
    await _refresh_readiness(session, gd)
    # Emit PARTICIPANT_JOINED on the event bus for other slices (publish never raises).
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
    # Local import: the auth repository is only needed for invitations.
    from services.auth import repository as auth_repo

    # Invitations only go to registered accounts.
    invitee = await auth_repo.get_user_by_email(session, email)
    if invitee is None:
        raise NotFoundError("no account with that email")

    # Seats can only be reserved while the lobby is open.
    if SessionState(gd.state) not in {SessionState.WAITING, SessionState.READY}:
        raise ConflictError("invitations can only be sent before the session starts")

    # Do not invite someone who already has a seat.
    existing = await repo.get_participant(session, gd.id, invitee.id)
    if existing is not None:
        raise ConflictError("that candidate is already in this session")

    # Respect capacity: invited seats count as active participants.
    if len(await repo.list_active_participants(session, gd.id)) >= gd.max_participants:
        raise ConflictError("this session is full")

    # INVITED reserves a label but does not count as present until the invitee joins.
    participant = Participant(
        session_id=gd.id,
        user_id=invitee.id,
        candidate_label=_next_label(await repo.used_labels(session, gd.id)),
        display_name=invitee.full_name,
        state=str(ParticipantState.INVITED),
    )
    await repo.add_participant(session, participant)
    # Send the in-app invitation with a link to the lobby.
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
    """Mark the user as having left; frees their readiness and emits an event."""
    # Only seated users can leave.
    participant = await require_participant(session, gd.id, user)
    # Leaving twice is a no-op.
    if ParticipantState(participant.state) is ParticipantState.LEFT:
        return
    # Mark LEFT and clear readiness so they no longer block the start condition.
    await _set_participant_state(session, participant, ParticipantState.LEFT)
    participant.is_ready = False
    participant.left_at = utcnow()
    await session.flush()
    # Someone leaving can make the start condition true or false.
    await _refresh_readiness(session, gd)
    # Announce PARTICIPANT_LEFT to other slices.
    await publish(
        Event.PARTICIPANT_LEFT, str(gd.id), {"candidate_label": participant.candidate_label}
    )


async def set_ready(session: AsyncSession, gd: GDSession, user: User, ready: bool) -> Participant:
    """Mark the user ready / not ready (FR-07) and re-check the start condition."""
    # Readiness is locked once the session starts.
    if SessionState(gd.state) not in {SessionState.WAITING, SessionState.READY}:
        raise ConflictError("readiness can only change before the session starts")
    participant = await require_participant(session, gd.id, user)
    # READY and JOINED are the two lobby states; un-ready goes back to JOINED.
    await _set_participant_state(
        session,
        participant,
        ParticipantState.READY if ready else ParticipantState.JOINED,
    )
    # Mirror the state in the boolean the lobby UI reads.
    participant.is_ready = ready
    await session.flush()
    # May move the whole session between WAITING and READY.
    await _refresh_readiness(session, gd)
    # Announce the change on the event bus.
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
    # Only people actually present count: neither LEFT nor merely INVITED.
    present = [
        p
        for p in participants
        if ParticipantState(p.state) not in {ParticipantState.LEFT, ParticipantState.INVITED}
    ]
    # Not enough people yet.
    if len(present) < gd.min_participants:
        return False
    # Everyone present must have pressed ready.
    return all(p.is_ready for p in present)


async def _refresh_readiness(session: AsyncSession, gd: GDSession) -> None:
    """Keep the session's WAITING ⇄ READY state in step with the lobby."""
    # Re-read the roster from the database so we judge the current truth.
    participants = await repo.list_participants(session, gd.id)
    met = start_condition_met(gd, participants)
    current = SessionState(gd.state)
    # Enough ready people: unlock the host's Start button (WAITING to READY).
    if met and current is SessionState.WAITING:
        await _set_session_state(session, gd, SessionState.READY)
    # Someone un-readied or left: lock it again (READY to WAITING).
    elif not met and current is SessionState.READY:
        await _set_session_state(session, gd, SessionState.WAITING)


async def lobby_view(session: AsyncSession, gd: GDSession) -> LobbyView:
    """FR-04."""
    # All seats, in label order.
    participants = await repo.list_participants(session, gd.id)
    # Left participants are hidden from the lobby.
    active = [p for p in participants if ParticipantState(p.state) is not ParticipantState.LEFT]
    # The snapshot the UI renders: counts, start condition and the roster.
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
    """READY → STARTING: begin preparation, notify everyone, and go live if prep is zero."""
    # Re-check the start condition on the server; never trust the client's Start click.
    participants = await repo.list_participants(session, gd.id)
    if not start_condition_met(gd, participants):
        raise ConflictError(
            f"start condition not met: need {gd.min_participants} ready participants"
        )
    # READY to STARTING: begin the preparation phase.
    await _set_session_state(session, gd, SessionState.STARTING)
    # The stored timestamp is what the server-authoritative timer counts from.
    gd.preparation_started_at = utcnow()
    await session.flush()
    # Tell other slices (websocket hub and others) that the session has started.
    await publish(
        Event.SESSION_STARTED,
        str(gd.id),
        {"phase": PHASE_PREPARATION, "preparation_seconds": gd.preparation_seconds},
    )
    # Notify everyone who has not left.
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
    # STARTING to LIVE.
    await _set_session_state(session, gd, SessionState.LIVE)
    # t=0 for the discussion; transcript and metric timestamps are relative to it.
    gd.started_at = utcnow()
    await session.flush()
    # Participants who were READY become ACTIVE speakers.
    for participant in await repo.list_active_participants(session, gd.id):
        if ParticipantState(participant.state) is ParticipantState.READY:
            await _set_participant_state(session, participant, ParticipantState.ACTIVE)
    # Announce the discussion phase together with its duration.
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
    # Ending twice is harmless: return without re-publishing events.
    if SessionState(gd.state) in {SessionState.ENDING, SessionState.ENDED}:
        return gd
    # The table forces LIVE to ENDING to ENDED, so both steps are taken in order.
    await _set_session_state(session, gd, SessionState.ENDING)
    await _set_session_state(session, gd, SessionState.ENDED)
    # Wall-clock end time for history and reports.
    gd.ended_at = utcnow()
    await session.flush()
    # Evaluation listens for this event instead of being called, so an AI failure cannot affect the
    # live session (invariant 1).
    await publish(Event.SESSION_ENDED, str(gd.id), {"reason": reason})
    # Tell participants their report is being prepared.
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
    """Mark the session FAILED (reachable from any state) and record why."""
    # Record why before changing state so the cause is never lost.
    gd.failure_reason = reason
    # Any state may move to FAILED.
    await _set_session_state(session, gd, SessionState.FAILED)
    return gd


# --------------------------------------------------------------------------
# Connection lifecycle — a disconnect must never end the session (design §9)
# --------------------------------------------------------------------------


async def mark_disconnected(session: AsyncSession, participant: Participant) -> Participant:
    """Record a dropped connection (counted, session keeps running)."""
    # Someone who left on purpose is not a dropped connection.
    if ParticipantState(participant.state) is ParticipantState.LEFT:
        return participant
    # DISCONNECTED, never LEFT: a drop must not end their participation.
    await _set_participant_state(session, participant, ParticipantState.DISCONNECTED)
    # A counter for the live UI; the metrics are recomputed from speech events.
    participant.connection_drops += 1
    participant.last_seen_at = utcnow()
    await session.flush()
    # Let the room know that this person dropped.
    await publish(
        Event.PARTICIPANT_DISCONNECTED,
        str(participant.session_id),
        {"candidate_label": participant.candidate_label},
    )
    return participant


async def mark_reconnected(session: AsyncSession, participant: Participant) -> Participant:
    """Bring a dropped participant back to ACTIVE and refresh last-seen."""
    # Only a dropped participant needs the RECONNECTED to ACTIVE hop.
    if ParticipantState(participant.state) is ParticipantState.DISCONNECTED:
        # Two steps because the table has no direct DISCONNECTED to ACTIVE edge.
        await _set_participant_state(session, participant, ParticipantState.RECONNECTED)
        await _set_participant_state(session, participant, ParticipantState.ACTIVE)
        await publish(
            Event.PARTICIPANT_RECONNECTED,
            str(participant.session_id),
            {"candidate_label": participant.candidate_label},
        )
    # Always refresh last-seen, even for a participant who never dropped.
    participant.last_seen_at = utcnow()
    await session.flush()
    return participant


# --------------------------------------------------------------------------
# FR-10 timer / FR-11 topic delivery
# --------------------------------------------------------------------------


def _elapsed(since: datetime | None, now: datetime | None = None) -> float:
    """Seconds between `since` and `now` (0 if `since` is unset)."""
    # Not started yet: zero elapsed rather than an error.
    if since is None:
        return 0.0
    # as_utc makes SQLite's naive datetimes comparable with timezone-aware ones.
    return ((now or utcnow()) - as_utc(since)).total_seconds()


def timer_view(gd: GDSession, now: datetime | None = None) -> TimerView:
    """Server-authoritative clock.

    Derived from stored timestamps rather than counted down in memory, so it
    survives a restart and every client agrees on the same number.
    """
    state = SessionState(gd.state)
    # Preparation countdown, measured from when STARTING began.
    if state is SessionState.STARTING:
        remaining = gd.preparation_seconds - _elapsed(gd.preparation_started_at, now)
        phase, duration = PHASE_PREPARATION, gd.preparation_seconds
    # Discussion countdown, measured from when the session went LIVE.
    elif state is SessionState.LIVE:
        remaining = gd.duration_seconds - _elapsed(gd.started_at, now)
        phase, duration = PHASE_DISCUSSION, gd.duration_seconds
    # Lobby: nothing is counting down, so show the configured preparation time.
    elif state in {SessionState.WAITING, SessionState.READY, SessionState.CREATED}:
        phase, duration, remaining = PHASE_WAITING, gd.preparation_seconds, gd.preparation_seconds
    # ENDING and later: the clock is finished.
    else:
        phase, duration, remaining = PHASE_ENDED, gd.duration_seconds, 0

    # Never show negative or fractional seconds.
    remaining = max(0, int(remaining))
    # warning turns on in the last WARNING_THRESHOLD_SECONDS of the discussion.
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
    # Before LIVE there is no discussion clock.
    if gd.started_at is None:
        return 0.0
    return max(0.0, _elapsed(gd.started_at, now))


def topic_view(gd: GDSession) -> TopicView:
    """FR-11.

    The topic is withheld until preparation begins, so nobody can prepare ahead
    of the others.
    """
    # The topic stays hidden through the lobby so nobody gets a head start.
    released = SessionState(gd.state) not in {
        SessionState.CREATED,
        SessionState.WAITING,
        SessionState.READY,
    }
    # Send an empty topic until it is released, but always the category and difficulty.
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
    # Only STARTING and LIVE are time-driven; every other state ignores the clock.
    state = SessionState(gd.state)
    if state is SessionState.STARTING:
        # Preparation is over: begin the discussion.
        if _elapsed(gd.preparation_started_at) >= gd.preparation_seconds:
            await go_live(session, gd)
    elif state is SessionState.LIVE:
        # Time is up: end the discussion.
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
    """Persist a speech event; same as `record_speech_tracked` but returns only the event."""
    # Convenience wrapper for callers that only need the event.
    return (await record_speech_tracked(session, gd, candidate_label, start, end))[0]


async def record_speech_tracked(
    session: AsyncSession,
    gd: GDSession,
    candidate_label: str,
    start: float,
    end: float,
    *,
    interrupted: str | None = None,
) -> tuple[SpeechEvent, list[Turn]]:
    """Persist a speech event and track the turn it makes.

    Returns the event and every Turn row that was created or newly marked as an
    interruption, so the caller can tell the room. The same path serves human
    speakers and AI personas, so both modes are tracked identically.
    """
    # Reject impossible intervals before touching the database.
    if end < start:
        raise InvalidStateTransition("speech event ends before it starts")
    # The raw speech interval: the input the deterministic metrics engine computes from (invariant
    # 4).
    event = SpeechEvent(session_id=gd.id, candidate_label=candidate_label, start=start, end=end)
    await repo.add_speech_event(session, event)

    # Turns of every other speaker; overlap with these decides interruptions and replies.
    others = [
        t for t in await repo.list_turns(session, gd.id) if t.candidate_label != candidate_label
    ]
    # The new turn; the interruption and reply labels are filled in below.
    turn = Turn(session_id=gd.id, candidate_label=candidate_label, start=start, end=end)
    # Collect every Turn row touched so the caller can tell the room.
    changed: list[Turn] = [turn]

    def overlap(a: Turn) -> float:
        """Seconds the existing turn `a` overlaps the new speech interval."""
        return min(a.end, end) - max(a.start, start)

    # Whoever started later while the other still held the floor is the interrupter.
    holders = [t for t in others if t.start < start and overlap(t) > MIN_OVERLAP_SECONDS]
    if interrupted:
        # The caller saw the floor being taken (practice barge-in) and says so.
        turn.interrupted_label = interrupted
    # Several speakers may overlap; the one who started most recently held the floor.
    elif holders:
        turn.interrupted_label = max(holders, key=lambda t: t.start).candidate_label
    else:
        # A speaker recorded late can retroactively be the one interrupted.
        for t in others:
            if t.start > start and t.interrupted_label is None and overlap(t) > MIN_OVERLAP_SECONDS:
                t.interrupted_label = candidate_label
                changed.append(t)
        # Not an interruption: see whether this speaker is replying to someone who finished just
        # before.
        previous = [
            t for t in others if t.end <= start and start - t.end <= RESPONSE_WINDOW_SECONDS
        ]
        # The reply target is whoever finished most recently within RESPONSE_WINDOW_SECONDS.
        if previous:
            turn.reply_to_label = max(previous, key=lambda t: t.end).candidate_label

    # Persist the turn right away so live displays and later evidence can read it.
    await repo.add_turn(session, turn)
    # Announce the finished speech on the event bus.
    await publish(
        Event.SPEECH_ENDED,
        str(gd.id),
        {"candidate_label": candidate_label, "start": start, "end": end},
    )
    return event, changed


async def interruption_times(
    session: AsyncSession, session_id: uuid.UUID
) -> dict[str, list[float]]:
    """Start time of every interruption each candidate made — the penalty's evidence."""
    # Maps each candidate label to the start times of the interruptions they made.
    times: dict[str, list[float]] = {}
    # Only turns flagged as interruptions count; the start time is the evidence timestamp.
    for turn in await repo.list_turns(session, session_id):
        if turn.interrupted_label is not None:
            times.setdefault(turn.candidate_label, []).append(turn.start)
    return times


async def list_open_lobbies(session: AsyncSession, limit: int, offset: int) -> list[LobbyListItem]:
    """The public lobby browser: group sessions still accepting students."""
    # Map query rows to API items and hide lobbies that are already full.
    return [
        LobbyListItem(
            session_id=gd.id,
            code=gd.code,
            topic=gd.topic,
            topic_category=gd.topic_category,
            gd_type=gd.gd_type,
            difficulty=gd.difficulty,
            host_name=host,
            occupancy=occupancy,
            capacity=gd.max_participants,
            duration_seconds=gd.duration_seconds,
            preparation_seconds=gd.preparation_seconds,
        )
        for gd, host, occupancy in await repo.list_open_lobbies(session, limit, offset)
        if occupancy < gd.max_participants
    ]
