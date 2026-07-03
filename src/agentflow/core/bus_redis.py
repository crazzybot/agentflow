"""Redis-backed TaskBus.

Key layout:
  run:{run_id}:dispatch   List — RPUSH enqueue, BLPOP dequeue (orchestrator → worker)
  run:{run_id}:result     List — RPUSH publish, BLPOP consume (worker → orchestrator)

Note: enqueue/dequeue/publish/consume are not yet wired into the orchestrator
engine (which drives agents directly today).  They are implemented here so the
bus is ready for a future Celery/worker-pool split without interface changes.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


class RedisTaskBus:
    """Task bus backed by Redis lists."""

    def __init__(self, redis: aioredis.Redis, ttl: int = 86_400) -> None:
        self._redis = redis
        self._ttl = ttl

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def create_run(self, run_id: str) -> None:
        logger.debug("Redis bus: registered run %s", run_id)

    def close_run(self, run_id: str) -> None:
        logger.debug("Redis bus: closed run %s", run_id)

    # ------------------------------------------------------------------
    # Dispatch  (orchestrator → worker)
    # ------------------------------------------------------------------

    async def enqueue_task(self, run_id: str, envelope: Any) -> None:
        data = envelope.model_dump_json() if hasattr(envelope, "model_dump_json") else json.dumps(envelope)
        key = f"run:{run_id}:dispatch"
        await self._redis.rpush(key, data)
        await self._redis.expire(key, self._ttl)

    async def dequeue_task(self, run_id: str) -> Any:
        result = await self._redis.blpop(f"run:{run_id}:dispatch", timeout=0)
        if result is None:
            return None
        _, data = result
        return json.loads(data)

    def task_done(self, run_id: str) -> None:
        pass  # No asyncio.Queue.task_done equivalent needed for Redis

    # ------------------------------------------------------------------
    # Results  (worker → orchestrator)
    # ------------------------------------------------------------------

    async def publish_result(self, run_id: str, result: Any) -> None:
        data = result.model_dump_json() if hasattr(result, "model_dump_json") else json.dumps(result)
        key = f"run:{run_id}:result"
        await self._redis.rpush(key, data)
        await self._redis.expire(key, self._ttl)

    async def consume_result(self, run_id: str) -> Any:
        result = await self._redis.blpop(f"run:{run_id}:result", timeout=0)
        if result is None:
            return None
        _, data = result
        return json.loads(data)
