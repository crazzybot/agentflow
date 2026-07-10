"""Shared async Redis client — one connection pool for the entire process."""
from __future__ import annotations

import redis.asyncio as aioredis

from agentflow.config import settings

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """Return a lazily-initialised, process-wide Redis client.

    The client uses a connection pool and decode_responses=True so all values
    come back as str rather than bytes — callers pass/receive plain JSON strings.
    """
    global _client
    if _client is None:
        _client = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=settings.redis_max_connections,
        )
    return _client


async def close_redis() -> None:
    """Close the connection pool.  Call once on application shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
