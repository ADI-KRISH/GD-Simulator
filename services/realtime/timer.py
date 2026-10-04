"""Server-authoritative session clock (FR-10, design §18).

One background task ticks every second, advances any session whose clock has run
out, and broadcasts the remaining time to that session's WebSocket room. Clients
render what they are told; they never decide that time is up.

The ticker is a convenience, not the source of truth — `advance_clock` is
idempotent and also runs lazily on API reads, so a restart mid-session cannot
strand a session in STARTING or LIVE.
"""

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.common.db import SessionFactory
from services.realtime.hub import hub
from services.realtime.protocol import ServerEvent, server_frame
from services.session import repository as repo
from services.session import service as session_service
from services.session.state import SessionState

logger = logging.getLogger(__name__)

TICK_SECONDS = 1.0

# States whose clock is running and therefore need ticking.
_TICKING_STATES = (SessionState.STARTING, SessionState.LIVE)


async def _tick_once(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as db:
        sessions = await repo.list_sessions_in_state(db, *_TICKING_STATES)
        for gd in sessions:
            before = SessionState(gd.state)
            await session_service.advance_clock(db, gd)
            after = SessionState(gd.state)
            timer = session_service.timer_view(gd)

            if after is not before:
                if after is SessionState.LIVE:
                    # Preparation is over: release the topic and start the clock.
                    await hub.broadcast(
                        str(gd.id),
                        server_frame(
                            ServerEvent.SESSION_STARTED, str(gd.id), {"phase": "discussion"}
                        ),
                    )
                    await hub.broadcast(
                        str(gd.id),
                        server_frame(
                            ServerEvent.TOPIC_RELEASED,
                            str(gd.id),
                            session_service.topic_view(gd).model_dump(mode="json"),
                        ),
                    )
                    await hub.broadcast(
                        str(gd.id),
                        server_frame(
                            ServerEvent.TIMER_STARTED, str(gd.id), timer.model_dump(mode="json")
                        ),
                    )
                elif after is SessionState.ENDED:
                    await hub.broadcast(
                        str(gd.id),
                        server_frame(
                            ServerEvent.SESSION_ENDED,
                            str(gd.id),
                            {"reason": "timer_elapsed"},
                        ),
                    )
            else:
                await hub.broadcast(
                    str(gd.id),
                    server_frame(
                        ServerEvent.TIMER_UPDATED, str(gd.id), timer.model_dump(mode="json")
                    ),
                )
        await db.commit()


async def run_ticker(
    factory: async_sessionmaker[AsyncSession] | None = None,
    *,
    interval: float = TICK_SECONDS,
) -> None:
    """Long-running loop. Cancelled on shutdown by the app lifespan.

    A tick failure is logged and the loop continues — a transient database blip
    must not silently stop every session clock on the machine.
    """
    factory = factory or SessionFactory
    logger.info("session timer ticker started", extra={"interval": interval})
    while True:
        try:
            await _tick_once(factory)
        except asyncio.CancelledError:
            logger.info("session timer ticker stopped")
            raise
        except Exception as exc:
            logger.warning("timer tick failed", extra={"error": str(exc)})
        await asyncio.sleep(interval)
