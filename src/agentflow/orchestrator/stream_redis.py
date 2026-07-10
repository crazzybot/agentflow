"""Redis Stream-backed SSE emitter.

Key layout:
  run:{run_id}:events   Redis Stream — one entry per SSEEvent plus a sentinel

emit() is synchronous (matching the in-memory interface) and fires an XADD
coroutine as a create_task() so the caller does not need to await it.  The
asyncio event loop serialises task_done callbacks in FIFO order, preserving
event ordering within a run.

The __aiter__ generator polls the stream with XREAD BLOCK 1000 (1-second
server-side timeout).  This lets asyncio.CancelledError propagate cleanly when
the HTTP client disconnects, and lets the generator check self.done so it exits
promptly after close() is called.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator

import redis.asyncio as aioredis

from agentflow.core.models import SSEEvent, SSEEventType, SSEPayload

logger = logging.getLogger(__name__)

_SENTINEL = "__done__"


class RedisStreamEmitter:
    """Publishes SSE events to a Redis Stream and exposes an async iterator."""

    def __init__(
        self,
        run_id: str,
        redis: aioredis.Redis,
        events_file: str | None = None,
        ttl: int = 86_400,
    ) -> None:
        self.run_id = run_id
        self._redis = redis
        self._ttl = ttl
        self.done = False
        self._seq = 0
        self._events_file = events_file
        self._stream_key = f"run:{run_id}:events"
        if events_file:
            os.makedirs(os.path.dirname(events_file), exist_ok=True)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def emit(
        self,
        event_type: SSEEventType,
        *,
        agent_id: str | None = None,
        message: str = "",
        data: Any = None,
    ) -> None:
        event = SSEEvent(
            run_id=self.run_id,
            seq=self._next_seq(),
            type=event_type,
            agent_id=agent_id,
            payload=SSEPayload(message=message, data=data),
        )
        event_json = json.dumps(event.model_dump(mode="json"))
        if self._events_file:
            with open(self._events_file, "a") as f:
                f.write(event_json + "\n")
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._xadd(event_json))
        except RuntimeError:
            logger.warning("[%s] emit called outside async context — event dropped", self.run_id)
        logger.debug("[%s] emit %s %s", self.run_id, event_type, message)

    async def _xadd(self, event_json: str) -> None:
        await self._redis.xadd(self._stream_key, {"data": event_json})

    def close(self) -> None:
        self.done = True
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._close_stream())
        except RuntimeError:
            logger.warning("[%s] close called outside async context", self.run_id)

    async def _close_stream(self) -> None:
        await self._redis.xadd(self._stream_key, {"data": _SENTINEL})
        await self._redis.expire(self._stream_key, self._ttl)

    async def __aiter__(self) -> AsyncIterator[dict[str, str]]:
        last_id = "0"  # start from first entry so reconnecting clients replay the stream
        while True:
            result = await self._redis.xread(
                streams={self._stream_key: last_id},
                block=1000,
                count=100,
            )
            if result:
                for _, messages in result:
                    for msg_id, fields in messages:
                        last_id = msg_id
                        payload = fields.get("data", "")
                        if payload == _SENTINEL:
                            return
                        yield {"data": payload}
            elif self.done:
                return


class RedisStreamRegistry:
    def __init__(self, redis: aioredis.Redis, ttl: int = 86_400) -> None:
        self._redis = redis
        self._ttl = ttl
        self._emitters: dict[str, RedisStreamEmitter] = {}

    def create(self, run_id: str, events_file: str | None = None) -> RedisStreamEmitter:
        emitter = RedisStreamEmitter(run_id, self._redis, events_file=events_file, ttl=self._ttl)
        self._emitters[run_id] = emitter
        return emitter

    def get(self, run_id: str) -> RedisStreamEmitter | None:
        """Synchronous local-only lookup — used by the start_run poll and _run_info."""
        return self._emitters.get(run_id)

    async def connect(self, run_id: str) -> RedisStreamEmitter | None:
        """Return an emitter for run_id, checking Redis on a local-cache miss.

        Called by the SSE route so that a replica that did not create the run
        can still attach to the Redis Stream and forward events to the client.
        The emitter is cached locally so repeated connections reuse the object.
        """
        if run_id in self._emitters:
            return self._emitters[run_id]
        # Check whether the stream key exists in Redis (created by another replica).
        stream_key = f"run:{run_id}:events"
        exists = await self._redis.exists(stream_key)
        if not exists:
            return None
        emitter = RedisStreamEmitter(run_id, self._redis, ttl=self._ttl)
        # Mark done if the sentinel is already in the stream (run finished before we connected).
        # XREVRANGE returns entries newest-first; we only need to check the last one.
        tail = await self._redis.xrevrange(stream_key, count=1)
        if tail:
            _, fields = tail[0]
            if fields and fields.get("data") == _SENTINEL:
                emitter.done = True
        self._emitters[run_id] = emitter
        return emitter

    def remove(self, run_id: str) -> None:
        self._emitters.pop(run_id, None)
