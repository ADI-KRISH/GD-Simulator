"""Redis connection for ephemeral state and the Redis Streams event bus.

Redis holds only ephemeral state (lobbies, connected users, timers). Durable
truth lives in PostgreSQL — see the consistency rule in claude.md.
"""

from redis.asyncio import Redis, from_url

from services.common.config import settings

_client: Redis | None = None


def get_redis() -> Redis:
    global _client
    if _client is None:
        _client = from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    return _client


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
