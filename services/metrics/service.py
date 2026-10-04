"""Metrics service: load speech events + transcript, compute, publish (FR-18)."""

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from services.common.events import Event, publish
from services.metrics.engine import SessionMetrics, compute_metrics
from services.session import repository as session_repo
from services.speech import repository as speech_repo
from services.speech.diarization import SpeakerSpan
from services.speech.service import to_contract

logger = logging.getLogger(__name__)


async def build_metrics(session: AsyncSession, session_id: uuid.UUID) -> SessionMetrics:
    """Recompute from scratch every time.

    Metrics are a pure function of stored speech events and transcript segments,
    so there is nothing to cache invalidate and no way for a cached value to
    drift from the evidence it claims to summarise.
    """
    events = await session_repo.list_speech_events(session, session_id)
    rows = await speech_repo.list_segments(session, session_id)
    participants = await session_repo.list_active_participants(session, session_id)

    spans = [
        SpeakerSpan(candidate_label=e.candidate_label, start=e.start, end=e.end) for e in events
    ]
    # Fall back to transcript timings when no microphone-activity events exist —
    # a session transcribed from imported audio still gets usable metrics.
    if not spans:
        spans = [
            SpeakerSpan(candidate_label=r.candidate_label, start=r.start, end=r.end) for r in rows
        ]

    return compute_metrics(
        session_id=str(session_id),
        spans=spans,
        segments=[to_contract(r) for r in rows],
        candidates=[p.candidate_label for p in participants],
    )


async def publish_metrics(session: AsyncSession, session_id: uuid.UUID) -> SessionMetrics:
    """Compute and announce (design §36 `metrics.updated`)."""
    metrics = await build_metrics(session, session_id)
    await publish(
        Event.METRICS_UPDATED,
        str(session_id),
        {
            "total_speaking_seconds": metrics.total_speaking_seconds,
            "participation": {
                label: m.participation_ratio for label, m in metrics.per_candidate.items()
            },
        },
    )
    return metrics
