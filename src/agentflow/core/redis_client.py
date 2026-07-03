"""Shared async Redis client — one connection pool for the entire process."""
from __future__ import annotations

import redis.asyncio as aioredis

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """Return a lazily-initialised, process-wide Redis client.

    The client uses a connection pool and decode_responses=True so all values
    come back as str rather than bytes — callers pass/receive plain JSON strings.
    """
    global _client
    if _client is None:
        from agentflow.config import settings
        _client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _client
