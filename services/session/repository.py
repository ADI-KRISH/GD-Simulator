"""Data access for sessions, participants and speech events. No business rules."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from services.auth.models import User
from services.session.models import GDSession, Participant, SpeechEvent, Turn
from services.session.state import ParticipantState, SessionState


async def get_session_by_id(session: AsyncSession, session_id: uuid.UUID) -> GDSession | None:
    return await session.get(GDSession, session_id)


async def get_session_by_code(session: AsyncSession, code: str) -> GDSession | None:
    result = await session.execute(select(GDSession).where(GDSession.code == code.upper()))
    return result.scalar_one_or_none()


async def code_exists(session: AsyncSession, code: str) -> bool:
    result = await session.execute(
        select(func.count()).select_from(GDSession).where(GDSession.code == code.upper())
    )
    return bool(result.scalar_one())


async def create_session(session: AsyncSession, gd: GDSession) -> GDSession:
    session.add(gd)
    await session.flush()
    await session.refresh(gd)
    return gd


async def list_sessions_for_host(
    session: AsyncSession, host_id: uuid.UUID, limit: int = 50, offset: int = 0
) -> list[GDSession]:
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
    result = await session.execute(
        select(GDSession)
        .join(Participant, Participant.session_id == GDSession.id)
        .where(Participant.user_id == user_id)
        .order_by(GDSession.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().unique())


async def list_sessions_in_state(session: AsyncSession, *states: SessionState) -> list[GDSession]:
    result = await session.execute(
        select(GDSession).where(GDSession.state.in_([str(s) for s in states]))
    )
    return list(result.scalars())


async def get_participant(
    session: AsyncSession, session_id: uuid.UUID, user_id: uuid.UUID
) -> Participant | None:
    result = await session.execute(
        select(Participant).where(
            Participant.session_id == session_id, Participant.user_id == user_id
        )
    )
    return result.scalar_one_or_none()


async def get_participant_by_label(
    session: AsyncSession, session_id: uuid.UUID, label: str
) -> Participant | None:
    result = await session.execute(
        select(Participant).where(
            Participant.session_id == session_id, Participant.candidate_label == label
        )
    )
    return result.scalar_one_or_none()


async def list_participants(session: AsyncSession, session_id: uuid.UUID) -> list[Participant]:
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
    session.add(participant)
    await session.flush()
    await session.refresh(participant)
    return participant


async def used_labels(session: AsyncSession, session_id: uuid.UUID) -> set[str]:
    result = await session.execute(
        select(Participant.candidate_label).where(Participant.session_id == session_id)
    )
    return set(result.scalars())


async def add_speech_event(session: AsyncSession, event: SpeechEvent) -> SpeechEvent:
    session.add(event)
    await session.flush()
    return event


async def list_speech_events(session: AsyncSession, session_id: uuid.UUID) -> list[SpeechEvent]:
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
    occupancy = (
        select(Participant.session_id, func.count().label("n"))
        .where(
            Participant.state.notin_([str(ParticipantState.LEFT), str(ParticipantState.INVITED)])
        )
        .group_by(Participant.session_id)
        .subquery()
    )
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
    session.add(turn)
    await session.flush()
    return turn


async def list_turns(session: AsyncSession, session_id: uuid.UUID) -> list[Turn]:
    result = await session.execute(
        select(Turn).where(Turn.session_id == session_id).order_by(Turn.start)
    )
    return list(result.scalars())
