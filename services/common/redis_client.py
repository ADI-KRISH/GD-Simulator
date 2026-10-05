"""Redis connection for ephemeral state and the Redis Streams event bus.

Redis holds only ephemeral state (lobbies, connected users, timers). Durable
truth lives in PostgreSQL — see the consistency rule in claude.md.
"""

from redis.asyncio import Redis, from_url

from services.common.config import settings

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
        return None

    def __getattr__(self, name: str):
        async def unavailable(*args, **kwargs):
            raise ConnectionError("redis is disabled (REDIS_URL=disabled)")

        return unavailable


_client: Redis | _DisabledRedis | None = None


def get_redis() -> Redis:
    global _client
    if _client is None:
        if settings.redis_url.strip().lower() == DISABLED:
            _client = _DisabledRedis()
        else:
            _client = from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    return _client  # type: ignore[return-value]


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def ping_redis() -> bool:
    try:
        return bool(await get_redis().ping())
    except Exception:
        return False
