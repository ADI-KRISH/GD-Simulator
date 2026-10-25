"""Observer loop driver (design §15).

Runs alongside a live session, folding each *closed* processing window into the
rolling state. Only closed windows are processed, and each is processed exactly
once — the `(session_id, chunk_index)` unique constraint makes a double-run a
no-op rather than a corrupted state.

This is the live half of the "two kinds of memory" split in design §47: raw
memory keeps accumulating in `transcript_segments` regardless of whether this
loop is running at all.

Purpose: The loop that runs beside every LIVE session, folds each closed 45-second window of
    transcript into the rolling state exactly once, stores it and pushes updates to the room.
Why needed: It is what makes the Observer live. Only closed windows are processed and a unique
    constraint makes re-runs harmless, while each failure is contained to one session so the
    live plane never notices an Observer problem (invariant 1).
Contains:
  - OBSERVER_INTERVAL_SECONDS / RECENT_IDEAS: how often to look for closed windows and how many
    ideas the room strip shows.
  - current_state: the latest stored rolling state, or an empty starting state.
  - process_pending_chunks: fold every closed, unprocessed window into the state and broadcast
    the result.
  - _tick: process pending windows for every LIVE session, isolating failures per session.
  - run_observer_loop: the long-running loop started by the app lifespan.
Used by: services/main.py
Depends on: services/common/contracts.py, services/common/db.py, services/common/events.py,
    services/metrics/__init__.py, services/observer/agent.py, services/realtime/hub.py,
    services/realtime/protocol.py, services/session/__init__.py, services/session/models.py,
    services/session/state.py, services/speech/__init__.py, services/speech/chunking.py,
    services/speech/models.py, services/speech/service.py
"""

import asyncio  # sleep between ticks, handle cancellation
import logging  # loop lifecycle and failure logs
import uuid  # session id type

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # DB session types

from services.common.contracts import RollingState  # the Observer's output contract
from services.common.db import SessionFactory  # default session factory
from services.common.events import Event, publish  # announce state.updated
from services.metrics import service as metrics_service  # fresh metrics for each window
from services.observer.agent import observe  # one LLM update step
from services.realtime.hub import hub  # push ideas/metrics to the room
from services.realtime.protocol import ServerEvent, server_frame  # WebSocket event frames
from services.session import repository as session_repo  # find LIVE sessions
from services.session import service as session_service  # elapsed discussion time
from services.session.models import GDSession  # session row type
from services.session.state import SessionState  # LIVE filter
from services.speech import repository as speech_repo  # transcript + stored rolling states
from services.speech.chunking import build_chunk, closed_chunk_indices  # 45s windows
from services.speech.models import RollingStateRow  # table for persisted states
from services.speech.service import to_contract  # DB row -> transcript contract

logger = logging.getLogger(__name__)

# How often to look for newly closed windows. Well under CHUNK_SECONDS so a
# window is picked up soon after it closes without busy-looping.
OBSERVER_INTERVAL_SECONDS = 10.0
# How many of the latest arguments the room's "recent ideas" strip shows.
RECENT_IDEAS = 3


async def current_state(session: AsyncSession, session_id: uuid.UUID) -> RollingState:
    """Latest rolling state, or the empty starting state (design §15 chunk 1)."""
    # The latest stored state, if any window has been processed yet.
    row = await speech_repo.latest_rolling_state(session, session_id)
    if row is None:
        # Before the first window there is an empty starting state.
        return RollingState.empty(str(session_id))
    return RollingState.model_validate(row.state)


async def process_pending_chunks(
    session: AsyncSession, gd: GDSession, *, up_to_end: bool = False
) -> int:
    """Fold every closed, unprocessed window into the state.

    `up_to_end=True` is used at session end to catch the final partial window,
    which is normally excluded because it is still filling.
    """
    session_id = gd.id
    # Always read the full raw transcript (invariant 3: the state supplements it, never replaces
    # it).
    rows = await speech_repo.list_segments(session, session_id)
    # No speech yet: nothing to process.
    if not rows:
        return 0
    segments = [to_contract(row) for row in rows]

    # At session end, include the final partial window too.
    if up_to_end:
        from services.speech.chunking import chunk_index_for

        wanted = list(range(chunk_index_for(max(s.start for s in segments)) + 1))
    # While live, only windows that have fully elapsed are processed.
    else:
        elapsed = session_service.elapsed_discussion_seconds(gd)
        wanted = closed_chunk_indices(elapsed)

    # Windows already folded in (the unique constraint makes a rerun a no-op).
    done = await speech_repo.processed_chunk_indices(session, session_id)
    # Closed windows not yet processed, oldest first.
    pending = [index for index in wanted if index not in done]
    if not pending:
        return 0

    # Measured numbers that ground the Observer's observations.
    metrics = (await metrics_service.build_metrics(session, session_id)).per_candidate
    # Start from the latest state and fold windows in order.
    state = await current_state(session, session_id)
    updated = 0

    for index in pending:
        chunk = build_chunk(segments, index)
        # One LLM step; it never raises and returns the previous state on failure.
        result = await observe(state, chunk, metrics, gd.topic, str(session_id))
        # Persist even an unchanged state: it records that this window was
        # considered, so the loop does not retry it forever.
        await speech_repo.add_rolling_state(
            session,
            RollingStateRow(
                session_id=session_id,
                chunk_index=index,
                state=result.state.model_dump(),
            ),
        )
        # Each window is committed as it is folded in, so the next window's LLM
        # call never runs inside a write transaction. On SQLite one held across
        # a rate-limited call locked out every other writer for minutes
        # (registration answered 500 while a report was being made).
        await session.commit()
        state = result.state
        # Announce only real updates; an unchanged state is stored but not broadcast.
        if result.changed:
            updated += 1
            # Internal event for other services.
            await publish(
                Event.STATE_UPDATED,
                str(session_id),
                {"chunk_index": index, "discussion_direction": state.discussion_direction},
            )
            # Live participation figures for the room's panel.
            await hub.broadcast(
                str(session_id),
                server_frame(
                    ServerEvent.METRICS_UPDATED,
                    str(session_id),
                    {
                        "chunk_index": index,
                        "participation": {
                            label: m.participation_ratio for label, m in metrics.items()
                        },
                    },
                ),
            )
            # FR-14.13: the room's "recent ideas" strip — observations, not scores.
            await hub.broadcast(
                str(session_id),
                server_frame(
                    ServerEvent.IDEAS_UPDATED,
                    str(session_id),
                    {
                        "chunk_index": index,
                        "ideas": state.main_arguments[-RECENT_IDEAS:],
                        "open_questions": state.unresolved_questions[-2:],
                    },
                ),
            )

    return updated


async def _tick(factory: async_sessionmaker[AsyncSession]) -> None:
    """Process pending windows for every LIVE session, isolating failures per session."""
    async with factory() as db:
        # Only LIVE sessions have a discussion to observe.
        live = await session_repo.list_sessions_in_state(db, SessionState.LIVE)
        for gd in live:
            # Isolate failures per session: one broken session must not stop the others.
            try:
                await process_pending_chunks(db, gd)
            except Exception as exc:
                # An observer failure is contained to its session and never
                # propagates to the live plane.
                logger.warning(
                    "observer loop failed for session",
                    extra={"session_id": str(gd.id), "error": str(exc)},
                )
        # Commit whatever succeeded across all sessions.
        await db.commit()


async def run_observer_loop(
    factory: async_sessionmaker[AsyncSession] | None = None,
    *,
    interval: float = OBSERVER_INTERVAL_SECONDS,
) -> None:
    """Long-running loop, started by the app lifespan and cancelled on shutdown."""
    # Tests pass their own factory; production uses the shared one.
    factory = factory or SessionFactory
    logger.info("observer loop started", extra={"interval": interval})
    # Runs until the app lifespan cancels it on shutdown.
    while True:
        try:
            await _tick(factory)
        # Cancellation is the normal shutdown path.
        except asyncio.CancelledError:
            logger.info("observer loop stopped")
            raise
        # Log and carry on; the loop must outlive transient failures.
        except Exception as exc:
            logger.warning("observer tick failed", extra={"error": str(exc)})
        # Wait before checking for newly closed windows.
        await asyncio.sleep(interval)
