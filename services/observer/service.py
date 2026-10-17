"""Observer loop driver (design §15).

Runs alongside a live session, folding each *closed* processing window into the
rolling state. Only closed windows are processed, and each is processed exactly
once — the `(session_id, chunk_index)` unique constraint makes a double-run a
no-op rather than a corrupted state.

This is the live half of the "two kinds of memory" split in design §47: raw
memory keeps accumulating in `transcript_segments` regardless of whether this
loop is running at all.
"""

import asyncio
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.common.contracts import RollingState
from services.common.db import SessionFactory
from services.common.events import Event, publish
from services.metrics import service as metrics_service
from services.observer.agent import observe
from services.realtime.hub import hub
from services.realtime.protocol import ServerEvent, server_frame
from services.session import repository as session_repo
from services.session import service as session_service
from services.session.models import GDSession
from services.session.state import SessionState
from services.speech import repository as speech_repo
from services.speech.chunking import build_chunk, closed_chunk_indices
from services.speech.models import RollingStateRow
from services.speech.service import to_contract

logger = logging.getLogger(__name__)

# How often to look for newly closed windows. Well under CHUNK_SECONDS so a
# window is picked up soon after it closes without busy-looping.
OBSERVER_INTERVAL_SECONDS = 10.0
# How many of the latest arguments the room's "recent ideas" strip shows.
RECENT_IDEAS = 3


async def current_state(session: AsyncSession, session_id: uuid.UUID) -> RollingState:
    """Latest rolling state, or the empty starting state (design §15 chunk 1)."""
    row = await speech_repo.latest_rolling_state(session, session_id)
    if row is None:
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
    rows = await speech_repo.list_segments(session, session_id)
    if not rows:
        return 0
    segments = [to_contract(row) for row in rows]

    if up_to_end:
        from services.speech.chunking import chunk_index_for

        wanted = list(range(chunk_index_for(max(s.start for s in segments)) + 1))
    else:
        elapsed = session_service.elapsed_discussion_seconds(gd)
        wanted = closed_chunk_indices(elapsed)

    done = await speech_repo.processed_chunk_indices(session, session_id)
    pending = [index for index in wanted if index not in done]
    if not pending:
        return 0

    metrics = (await metrics_service.build_metrics(session, session_id)).per_candidate
    state = await current_state(session, session_id)
    updated = 0

    for index in pending:
        chunk = build_chunk(segments, index)
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
        if result.changed:
            updated += 1
            await publish(
                Event.STATE_UPDATED,
                str(session_id),
                {"chunk_index": index, "discussion_direction": state.discussion_direction},
            )
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
    async with factory() as db:
        live = await session_repo.list_sessions_in_state(db, SessionState.LIVE)
        for gd in live:
            try:
                await process_pending_chunks(db, gd)
            except Exception as exc:
                # An observer failure is contained to its session and never
                # propagates to the live plane.
                logger.warning(
                    "observer loop failed for session",
                    extra={"session_id": str(gd.id), "error": str(exc)},
                )
        await db.commit()


async def run_observer_loop(
    factory: async_sessionmaker[AsyncSession] | None = None,
    *,
    interval: float = OBSERVER_INTERVAL_SECONDS,
) -> None:
    """Long-running loop, started by the app lifespan and cancelled on shutdown."""
    factory = factory or SessionFactory
    logger.info("observer loop started", extra={"interval": interval})
    while True:
        try:
            await _tick(factory)
        except asyncio.CancelledError:
            logger.info("observer loop stopped")
            raise
        except Exception as exc:
            logger.warning("observer tick failed", extra={"error": str(exc)})
        await asyncio.sleep(interval)
