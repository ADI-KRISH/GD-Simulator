"""Data access for sessions, participants and speech events. No business rules."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from services.session.models import GDSession, Participant, SpeechEvent
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
