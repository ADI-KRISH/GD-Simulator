"""Redis connection for ephemeral state and the Redis Streams event bus.

Redis holds only ephemeral state (lobbies, connected users, timers). Durable
truth lives in PostgreSQL — see the consistency rule in claude.md.

Purpose:    create, share and close one async Redis client for the whole process.
Why needed: events (Redis Streams) and the evaluation queue go through Redis;
            without one shared client every module would open its own
            connection, and "no Docker" mode would have nowhere to switch Redis
            off cleanly.
Contains:
    DISABLED        - the REDIS_URL value that turns Redis off.
    _DisabledRedis  - stand-in client whose every command fails instantly, so
                      callers fall back fast instead of waiting on a timeout.
    get_redis       - lazily builds the shared client.
    close_redis     - shutdown hook that closes and resets it.
    ping_redis      - used by the /health check.
Used by:    common/events, evaluation/queue, main, tests/conftest.
Depends on: common/config (the redis_url setting).
"""

from redis.asyncio import Redis, from_url  # async client type and URL-based constructor

from services.common.config import settings  # provides redis_url

# REDIS_URL=disabled runs the platform with no Redis at all (no-Docker mode).
DISABLED = "disabled"


class _DisabledRedis:
    """Stands in for a client when Redis is switched off.

    Every call fails at once instead of waiting on a connect timeout — on
    Windows a refused localhost connection costs seconds, and events are
    published on every turn. Each caller already treats a Redis failure as
    non-fatal: events are dropped, and the evaluation worker falls back to the
    job rows in the database.
    """

    async def aclose(self) -> None:
        """Nothing to close; matches the real client's shutdown method."""
        return None

    def __getattr__(self, name: str):
        """Turn any Redis command (ping, xadd, ...) into a coroutine that fails fast."""

        # Callers `await` the command, so hand back an async function that raises.
        async def unavailable(*args, **kwargs):
            raise ConnectionError("redis is disabled (REDIS_URL=disabled)")

        return unavailable


# Process-wide client, created lazily by get_redis().
_client: Redis | _DisabledRedis | None = None


def get_redis() -> Redis:
    """Return the shared Redis client (or the fail-fast stand-in when disabled)."""
    global _client
    # Build the client on first use so importing this module never connects.
    if _client is None:
        if settings.redis_url.strip().lower() == DISABLED:
            _client = _DisabledRedis()
        else:
            _client = from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    return _client  # type: ignore[return-value]


async def close_redis() -> None:
    """Close the shared client on shutdown and reset it."""
    global _client
    # Reset to None so a later get_redis() (e.g. in tests) starts fresh.
    if _client is not None:
        await _client.aclose()
        _client = None


async def ping_redis() -> bool:
    """Health check: True if Redis answers a PING, False on any failure."""
    # Any error means "not healthy"; /health must never raise.
    try:
        return bool(await get_redis().ping())
    except Exception:
        return False
