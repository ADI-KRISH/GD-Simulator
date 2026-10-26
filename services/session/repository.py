"""Data access for sessions, participants and speech events. No business rules.

Purpose: All database queries for sessions, participants, speech events and turns. It holds no
    business rules.
Why needed: Putting SQL in one place lets service.py and the background loops ask questions like
    which sessions are LIVE without each writing its own query, and lets tests swap the
    database.
Contains:
  - get_session_by_id / get_session_by_code / code_exists: find a session or check a join code
    is free.
  - create_session: insert a new session.
  - list_sessions_for_host / list_sessions_for_participant: a user's history, newest first.
  - list_sessions_in_state: lets the ticker and workers find sessions in given states.
  - get_participant / get_participant_by_label / list_participants / list_active_participants /
    add_participant / used_labels: seat lookups and label allocation.
  - add_speech_event / list_speech_events: store and replay raw speech for the metrics engine.
  - add_turn / list_turns: store and read tracked speaking turns.
  - OPEN_SESSION_STATES / active_session_for_user: find the unfinished session a user is already
    sitting in.
  - list_open_lobbies: group lobbies still accepting people, with host name and occupancy.
Used by: nothing imports it directly (entry point or loaded by the framework)
Depends on: services/auth/models.py, services/session/models.py, services/session/state.py
"""

import uuid  # id types

from sqlalchemy import func, select  # count() and query building
from sqlalchemy.ext.asyncio import AsyncSession  # database session

from services.auth.models import User  # host names in the lobby browser
from services.session.models import GDSession, Participant, SpeechEvent, Turn  # tables
from services.session.state import ParticipantState, SessionState  # state filters


async def get_session_by_id(session: AsyncSession, session_id: uuid.UUID) -> GDSession | None:
    """Load a GD session by primary key."""
    return await session.get(GDSession, session_id)


async def get_session_by_code(session: AsyncSession, code: str) -> GDSession | None:
    """Find a session by its join code (codes are stored uppercase)."""
    # Join codes are stored uppercase, so compare in uppercase to make entry case-insensitive.
    result = await session.execute(select(GDSession).where(GDSession.code == code.upper()))
    return result.scalar_one_or_none()


async def code_exists(session: AsyncSession, code: str) -> bool:
    """True if a session already uses this join code (used to avoid collisions)."""
    # count() is cheaper than loading the row when we only need yes or no.
    result = await session.execute(
        select(func.count()).select_from(GDSession).where(GDSession.code == code.upper())
    )
    return bool(result.scalar_one())


async def create_session(session: AsyncSession, gd: GDSession) -> GDSession:
    """Insert a new session and return it refreshed from the database."""
    # Flush inside the open transaction so defaults are assigned, then refresh to load them onto the
    # object.
    session.add(gd)
    await session.flush()
    await session.refresh(gd)
    return gd


async def list_sessions_for_host(
    session: AsyncSession, host_id: uuid.UUID, limit: int = 50, offset: int = 0
) -> list[GDSession]:
    """Sessions created by a host, newest first, paginated."""
    # Newest first, with limit and offset for paging.
    result = await session.execute(
        select(GDSession)
        .where(GDSession.host_id == host_id)
        .order_by(GDSession.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars())


async def list_sessions_for_participant(
    session: AsyncSession, user_id: uuid.UUID, limit: int = 50, offset: int = 0
) -> list[GDSession]:
    """Sessions a user has taken part in, newest first, paginated."""
    # Join through participants to find sessions where this user holds a seat.
    result = await session.execute(
        select(GDSession)
        .join(Participant, Participant.session_id == GDSession.id)
        .where(Participant.user_id == user_id)
        .order_by(GDSession.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    # unique() removes duplicate sessions after the join.
    return list(result.scalars().unique())


async def list_sessions_in_state(session: AsyncSession, *states: SessionState) -> list[GDSession]:
    """All sessions currently in any of the given states (used by background loops)."""
    # States are stored as plain strings, so convert the enum members before filtering.
    result = await session.execute(
        select(GDSession).where(GDSession.state.in_([str(s) for s in states]))
    )
    return list(result.scalars())


async def get_participant(
    session: AsyncSession, session_id: uuid.UUID, user_id: uuid.UUID
) -> Participant | None:
    """Find a user's seat in a session, if they have one."""
    # The (session_id, user_id) unique constraint guarantees at most one row.
    result = await session.execute(
        select(Participant).where(
            Participant.session_id == session_id, Participant.user_id == user_id
        )
    )
    return result.scalar_one_or_none()


async def get_participant_by_label(
    session: AsyncSession, session_id: uuid.UUID, label: str
) -> Participant | None:
    """Find a participant by their short candidate label ("A", "B", ...)."""
    # Labels are unique within a session, so this finds at most one participant.
    result = await session.execute(
        select(Participant).where(
            Participant.session_id == session_id, Participant.candidate_label == label
        )
    )
    return result.scalar_one_or_none()


async def list_participants(session: AsyncSession, session_id: uuid.UUID) -> list[Participant]:
    """Every participant of a session, ordered by label."""
    result = await session.execute(
        select(Participant)
        .where(Participant.session_id == session_id)
        .order_by(Participant.candidate_label)
    )
    return list(result.scalars())


async def list_active_participants(
    session: AsyncSession, session_id: uuid.UUID
) -> list[Participant]:
    """Everyone who has not left — disconnected candidates still count (design §9)."""
    # Only LEFT is excluded: a DISCONNECTED candidate can still reconnect (design §9).
    result = await session.execute(
        select(Participant)
        .where(
            Participant.session_id == session_id,
            Participant.state != str(ParticipantState.LEFT),
        )
        .order_by(Participant.candidate_label)
    )
    return list(result.scalars())


async def add_participant(session: AsyncSession, participant: Participant) -> Participant:
    """Insert a participant and return it refreshed from the database."""
    session.add(participant)
    await session.flush()
    await session.refresh(participant)
    return participant


async def used_labels(session: AsyncSession, session_id: uuid.UUID) -> set[str]:
    """Candidate labels already taken in a session, so the next one is unique."""
    # Fetch just the label column; the caller picks the next free letter.
    result = await session.execute(
        select(Participant.candidate_label).where(Participant.session_id == session_id)
    )
    return set(result.scalars())


async def add_speech_event(session: AsyncSession, event: SpeechEvent) -> SpeechEvent:
    """Persist one raw speech start/stop interval."""
    session.add(event)
    await session.flush()
    return event


async def list_speech_events(session: AsyncSession, session_id: uuid.UUID) -> list[SpeechEvent]:
    """All speech intervals of a session in time order (input to the metrics engine)."""
    # Time order matters: the metrics engine walks the events chronologically.
    result = await session.execute(
        select(SpeechEvent).where(SpeechEvent.session_id == session_id).order_by(SpeechEvent.start)
    )
    return list(result.scalars())


# Sessions that still hold their participants (nobody is "in" a finished one).
OPEN_SESSION_STATES = (
    SessionState.CREATED,
    SessionState.WAITING,
    SessionState.READY,
    SessionState.STARTING,
    SessionState.LIVE,
    SessionState.ENDING,
)


async def active_session_for_user(session: AsyncSession, user_id: uuid.UUID) -> GDSession | None:
    """The unfinished session this user is sitting in, if any.

    An INVITED seat is only a reservation, so it does not count; neither does a
    seat the user has left.
    """
    # Join participants to sessions, ignoring left or merely invited seats and finished sessions.
    result = await session.execute(
        select(GDSession)
        .join(Participant, Participant.session_id == GDSession.id)
        .where(
            Participant.user_id == user_id,
            Participant.state.notin_([str(ParticipantState.LEFT), str(ParticipantState.INVITED)]),
            GDSession.state.in_([str(s) for s in OPEN_SESSION_STATES]),
        )
        .limit(1)
    )
    return result.scalars().first()


async def list_open_lobbies(
    session: AsyncSession, limit: int = 50, offset: int = 0
) -> list[tuple[GDSession, str, int]]:
    """Group lobbies still accepting people: (session, host name, occupancy)."""
    # Subquery: how many seated (not left/invited) people each session has.
    occupancy = (
        select(Participant.session_id, func.count().label("n"))
        .where(
            Participant.state.notin_([str(ParticipantState.LEFT), str(ParticipantState.INVITED)])
        )
        .group_by(Participant.session_id)
        .subquery()
    )
    # The outer join keeps empty lobbies (occupancy 0); only group sessions still waiting or ready
    # are listed.
    result = await session.execute(
        select(GDSession, User.full_name, func.coalesce(occupancy.c.n, 0))
        .join(User, User.id == GDSession.host_id)
        .outerjoin(occupancy, occupancy.c.session_id == GDSession.id)
        .where(
            GDSession.mode == "group",
            GDSession.state.in_([str(SessionState.WAITING), str(SessionState.READY)]),
        )
        .order_by(GDSession.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return [(gd, host, int(n)) for gd, host, n in result.all()]


async def add_turn(session: AsyncSession, turn: Turn) -> Turn:
    """Persist one tracked speaking turn."""
    session.add(turn)
    await session.flush()
    return turn


async def list_turns(session: AsyncSession, session_id: uuid.UUID) -> list[Turn]:
    """All turns of a session in time order."""
    # Time order, like speech events.
    result = await session.execute(
        select(Turn).where(Turn.session_id == session_id).order_by(Turn.start)
    )
    return list(result.scalars())
