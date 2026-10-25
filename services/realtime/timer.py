"""Server-authoritative session clock (FR-10, design §18).

One background task ticks every second, advances any session whose clock has run
out, and broadcasts the remaining time to that session's WebSocket room. Clients
render what they are told; they never decide that time is up.

The ticker is a convenience, not the source of truth — `advance_clock` is
idempotent and also runs lazily on API reads, so a restart mid-session cannot
strand a session in STARTING or LIVE.

Purpose: A background ticker that advances every running session's clock once a second and
    broadcasts the remaining time to that session's room.
Why needed: Clients only render the countdown; the server decides when preparation ends and when
    the discussion is over. The ticker makes those transitions happen on time even if nobody is
    calling the API.
Contains:
  - TICK_SECONDS / _TICKING_STATES: how often to tick and which states have a running clock.
  - announce_discussion: tell the room the phase, topic and clock when preparation finishes.
  - _tick_once: advance each STARTING or LIVE session and broadcast what changed.
  - run_ticker: the long-running loop started by the app lifespan; it survives tick failures.
Used by: services/main.py, services/practice/engine.py
Depends on: services/common/db.py, services/realtime/hub.py, services/realtime/protocol.py,
    services/session/__init__.py, services/session/state.py
"""

import asyncio  # sleep between ticks, handle cancellation
import logging  # tick lifecycle and failure logs

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # DB session factory types

from services.common.db import SessionFactory  # default session factory
from services.realtime.hub import hub  # broadcast timer updates to rooms
from services.realtime.protocol import ServerEvent, server_frame  # WebSocket event frames
from services.session import repository as repo  # find sessions with running clocks
from services.session import service as session_service  # advance_clock / timer_view
from services.session.state import SessionState  # detect state changes

logger = logging.getLogger(__name__)

# How often the clock advances and clients are updated.
TICK_SECONDS = 1.0

# States whose clock is running and therefore need ticking.
_TICKING_STATES = (SessionState.STARTING, SessionState.LIVE)


async def announce_discussion(gd) -> None:
    """Preparation is over: tell the room — the phase, the topic, the clock.

    The ticker calls this when it sees a session move to LIVE. A session with no
    preparation is LIVE the moment it starts, before any tick can see the move,
    so whoever starts it calls this directly; otherwise the room is never told,
    and a practice page stayed on "Preparation" with its microphone deaf.
    """
    # The hub keys rooms by string session id.
    session_id = str(gd.id)
    # Step 1: phase change, the discussion has started.
    await hub.broadcast(
        session_id,
        server_frame(ServerEvent.SESSION_STARTED, session_id, {"phase": "discussion"}),
    )
    # Step 2: release the topic now that preparation is over (FR-11).
    await hub.broadcast(
        session_id,
        server_frame(
            ServerEvent.TOPIC_RELEASED,
            session_id,
            session_service.topic_view(gd).model_dump(mode="json"),
        ),
    )
    # Step 3: start the discussion clock on every client.
    await hub.broadcast(
        session_id,
        server_frame(
            ServerEvent.TIMER_STARTED,
            session_id,
            session_service.timer_view(gd).model_dump(mode="json"),
        ),
    )


async def _tick_once(factory: async_sessionmaker[AsyncSession]) -> None:
    """Advance every running session's clock once and tell each room what changed."""
    # Each tick uses its own short-lived database session.
    async with factory() as db:
        # Only sessions whose clock is running (STARTING or LIVE) need ticking.
        sessions = await repo.list_sessions_in_state(db, *_TICKING_STATES)
        for gd in sessions:
            # Remember the state so we can tell whether this tick changed the phase.
            before = SessionState(gd.state)
            # Apply any transition the clock says is due (STARTING to LIVE, LIVE to ended).
            await session_service.advance_clock(db, gd)
            after = SessionState(gd.state)
            # The server-authoritative remaining time to send to clients.
            timer = session_service.timer_view(gd)

            # A phase change gets its own announcement instead of a plain tick.
            if after is not before:
                # Preparation finished: announce phase, topic and clock.
                if after is SessionState.LIVE:
                    await announce_discussion(gd)
                # Time ran out: tell the room the discussion is over.
                elif after is SessionState.ENDED:
                    await hub.broadcast(
                        str(gd.id),
                        server_frame(
                            ServerEvent.SESSION_ENDED,
                            str(gd.id),
                            {"reason": "timer_elapsed"},
                        ),
                    )
            # No change: just broadcast the countdown.
            else:
                await hub.broadcast(
                    str(gd.id),
                    server_frame(
                        ServerEvent.TIMER_UPDATED, str(gd.id), timer.model_dump(mode="json")
                    ),
                )
        # Persist every transition made during this tick in one commit.
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
    # Tests pass their own factory; production uses the shared one.
    factory = factory or SessionFactory
    logger.info("session timer ticker started", extra={"interval": interval})
    # Runs until the app lifespan cancels it on shutdown.
    while True:
        try:
            await _tick_once(factory)
        # Cancellation is the normal shutdown path: log it and re-raise.
        except asyncio.CancelledError:
            logger.info("session timer ticker stopped")
            raise
        # Any other failure is logged and the loop keeps going.
        except Exception as exc:
            logger.warning("timer tick failed", extra={"error": str(exc)})
        # Wait for the next tick.
        await asyncio.sleep(interval)
