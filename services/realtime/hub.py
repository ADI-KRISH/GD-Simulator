"""In-process WebSocket connection registry (design §10).

One hub per API process, keyed by session. The local deployment runs a single
backend instance, so a plain in-memory registry is the whole fan-out layer — the
Redis Pub/Sub fan-out in design §42 belongs to the scale target and is
deliberately not built here (claude.md "Deployment — LOCAL ONLY").

Broadcast never raises: a client that has gone away must not break the loop that
is serving everyone else in the room.
"""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


# eq=False so connections hash by identity: two sockets from the same user in
# two browser tabs are genuinely different connections and both belong in the
# room's set.
@dataclass(slots=True, eq=False)
class Connection:
    websocket: WebSocket
    session_id: str
    user_id: str
    candidate_label: str


@dataclass
class Hub:
    _rooms: dict[str, set[Connection]] = field(default_factory=lambda: defaultdict(set))
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def connect(self, connection: Connection) -> None:
        async with self._lock:
            self._rooms[connection.session_id].add(connection)

    async def disconnect(self, connection: Connection) -> None:
        async with self._lock:
            room = self._rooms.get(connection.session_id)
            if room is None:
                return
            room.discard(connection)
            if not room:
                self._rooms.pop(connection.session_id, None)

    def members(self, session_id: str) -> list[Connection]:
        return list(self._rooms.get(session_id, ()))

    def occupancy(self, session_id: str) -> int:
        return len(self._rooms.get(session_id, ()))

    def active_sessions(self) -> list[str]:
        return list(self._rooms)

    def reset(self) -> None:
        """Drop every room. Used by tests; the hub is a process-wide singleton."""
        self._rooms.clear()

    def is_connected(self, session_id: str, user_id: str) -> bool:
        return any(c.user_id == user_id for c in self.members(session_id))

    async def send(self, connection: Connection, message: dict[str, Any]) -> bool:
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
        targets = [
            c for c in self.members(session_id) if exclude_user is None or c.user_id != exclude_user
        ]
        if not targets:
            return 0
        results = await asyncio.gather(
            *(self.send(c, message) for c in targets), return_exceptions=True
        )
        return sum(1 for r in results if r is True)


hub = Hub()
