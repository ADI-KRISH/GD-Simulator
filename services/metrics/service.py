"""Metrics service: load speech events + transcript, compute, publish (FR-18).

Purpose: Loads a session's speech events, transcript and roster from the database, runs the
    metrics engine on them and announces the result.
Why needed: The engine is pure and knows nothing about the database; this thin layer does the
    I/O so the engine stays easy to test and metrics are always recomputed from the stored
    evidence.
Contains:
  - build_metrics: gather the inputs and return fresh SessionMetrics (never cached).
  - publish_metrics: build, then emit the metrics.updated event with a participation summary.
Used by: services/metrics/router.py
Depends on: services/common/events.py, services/metrics/engine.py, services/session/__init__.py,
    services/speech/__init__.py, services/speech/diarization.py, services/speech/service.py
"""

import logging  # module logger
import uuid  # session id type

from sqlalchemy.ext.asyncio import AsyncSession  # database session

from services.common.events import Event, publish  # announce metrics.updated
from services.metrics.engine import SessionMetrics, compute_metrics  # the pure computation
from services.session import repository as session_repo  # speech events, participants
from services.speech import repository as speech_repo  # transcript segments
from services.speech.diarization import SpeakerSpan  # who-spoke-when interval type
from services.speech.service import to_contract  # DB row -> TranscriptSegment contract

logger = logging.getLogger(__name__)


async def build_metrics(session: AsyncSession, session_id: uuid.UUID) -> SessionMetrics:
    """Recompute from scratch every time.

    Metrics are a pure function of stored speech events and transcript segments,
    so there is nothing to cache invalidate and no way for a cached value to
    drift from the evidence it claims to summarise.
    """
    # Microphone activity: the ground truth for who held the floor.
    events = await session_repo.list_speech_events(session, session_id)
    # Transcript text: the only source of the word-based counts.
    rows = await speech_repo.list_segments(session, session_id)
    # The roster, so silent candidates still appear with zero values.
    participants = await session_repo.list_active_participants(session, session_id)

    # Convert database rows into the span type the pure engine understands.
    spans = [
        SpeakerSpan(candidate_label=e.candidate_label, start=e.start, end=e.end) for e in events
    ]
    # Fall back to transcript timings when no microphone-activity events exist —
    # a session transcribed from imported audio still gets usable metrics.
    if not spans:
        spans = [
            SpeakerSpan(candidate_label=r.candidate_label, start=r.start, end=r.end) for r in rows
        ]

    # Hand everything to the pure function; this module does I/O only.
    return compute_metrics(
        session_id=str(session_id),
        spans=spans,
        segments=[to_contract(r) for r in rows],
        candidates=[p.candidate_label for p in participants],
    )


async def publish_metrics(session: AsyncSession, session_id: uuid.UUID) -> SessionMetrics:
    """Compute and announce (design §36 `metrics.updated`)."""
    metrics = await build_metrics(session, session_id)
    # Announce the update on the event bus; the payload carries just a summary.
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
