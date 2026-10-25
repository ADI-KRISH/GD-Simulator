"""In-process WebSocket connection registry (design §10).

One hub per API process, keyed by session. The local deployment runs a single
backend instance, so a plain in-memory registry is the whole fan-out layer — the
Redis Pub/Sub fan-out in design §42 belongs to the scale target and is
deliberately not built here (claude.md "Deployment — LOCAL ONLY").

Broadcast never raises: a client that has gone away must not break the loop that
is serving everyone else in the room.

Purpose: An in-memory registry of open WebSocket connections, grouped into one room per session,
    with safe send and broadcast helpers.
Why needed: Routes, the timer ticker and the practice engine all need to push events to everyone
    in a room; one shared hub means they do not each track sockets, and a dead client can never
    break delivery to the others.
Contains:
  - Connection: one open socket plus the user and candidate label it belongs to.
  - Hub.connect / disconnect: add and remove a connection, dropping empty rooms.
  - Hub.members / occupancy / active_sessions / is_connected: read-only views of who is in which
    room.
  - Hub.send: deliver JSON to one connection, returning False instead of raising.
  - Hub.broadcast: deliver to a whole room, optionally skipping one user.
  - hub: the process-wide singleton everyone imports.
Used by: services/evaluation/service.py, services/lobby/router.py,
    services/notifications/service.py, services/observer/service.py,
    services/practice/engine.py, services/practice/router.py, services/realtime/timer.py,
    services/realtime/ws.py, services/session/router.py, services/simulation.py,
    services/speech/router.py
Depends on: nothing inside the project (standard library / third-party only)
"""

import asyncio  # lock and concurrent sends
import logging  # debug-log failed sends
from collections import defaultdict  # rooms default to an empty set
from dataclasses import dataclass, field  # Connection and Hub containers
from typing import Any  # JSON message payloads

from fastapi import WebSocket  # the live socket held by each connection

logger = logging.getLogger(__name__)


# eq=False so connections hash by identity: two sockets from the same user in
# two browser tabs are genuinely different connections and both belong in the
# room's set.
@dataclass(slots=True, eq=False)
class Connection:
    """One open WebSocket, tagged with who it belongs to."""

    websocket: WebSocket
    session_id: str
    user_id: str
    candidate_label: str


@dataclass
class Hub:
    """Registry of live connections grouped into one room per session."""

    _rooms: dict[str, set[Connection]] = field(default_factory=lambda: defaultdict(set))
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # guards room changes

    async def connect(self, connection: Connection) -> None:
        """Add a connection to its session's room."""
        # The lock stops two sockets joining at once from corrupting the room set.
        async with self._lock:
            self._rooms[connection.session_id].add(connection)

    async def disconnect(self, connection: Connection) -> None:
        """Remove a connection, and drop the room once it is empty."""
        # Same lock as connect, so room membership changes never interleave.
        async with self._lock:
            # Tolerate a double disconnect: a room that is already gone is simply ignored.
            room = self._rooms.get(connection.session_id)
            if room is None:
                return
            room.discard(connection)
            # Drop empty rooms so the registry does not grow forever.
            if not room:
                self._rooms.pop(connection.session_id, None)

    def members(self, session_id: str) -> list[Connection]:
        """Snapshot of the connections currently in a session's room."""
        return list(self._rooms.get(session_id, ()))

    def occupancy(self, session_id: str) -> int:
        """How many connections the room holds."""
        return len(self._rooms.get(session_id, ()))

    def active_sessions(self) -> list[str]:
        """Ids of all sessions that have at least one connection."""
        return list(self._rooms)

    def reset(self) -> None:
        """Drop every room. Used by tests; the hub is a process-wide singleton."""
        self._rooms.clear()

    def is_connected(self, session_id: str, user_id: str) -> bool:
        """True if the user has any open connection in the session."""
        return any(c.user_id == user_id for c in self.members(session_id))

    async def send(self, connection: Connection, message: dict[str, Any]) -> bool:
        """Send JSON to one connection; return False (never raise) if it has gone away."""
        # A dead socket raises on send; swallow it so one bad client cannot affect the others.
        try:
            await connection.websocket.send_json(message)
            return True
        except Exception as exc:
            logger.debug(
                "websocket send failed",
                extra={"session_id": connection.session_id, "error": str(exc)},
            )
            return False

    async def broadcast(
        self, session_id: str, message: dict[str, Any], *, exclude_user: str | None = None
    ) -> int:
        """Send a message to everyone in the room (optionally skipping one user).

        Returns how many connections received it.
        """
        # Everyone in the room except an optionally excluded user.
        targets = [
            c for c in self.members(session_id) if exclude_user is None or c.user_id != exclude_user
        ]
        # Nothing to send: avoid calling gather with no tasks.
        if not targets:
            return 0
        # Send to all connections concurrently; return_exceptions stops one failure cancelling the
        # rest.
        results = await asyncio.gather(
            *(self.send(c, message) for c in targets), return_exceptions=True
        )
        # Count successful deliveries only (send returns True on success).
        return sum(1 for r in results if r is True)


# Process-wide singleton imported by routers, the ticker and the WebSocket handler.
hub = Hub()
