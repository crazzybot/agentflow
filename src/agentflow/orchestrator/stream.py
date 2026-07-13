"""SSE stream emitter — per-run event channel consumed by the HTTP layer."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator

from agentflow.config import settings
from agentflow.core.models import SSEEvent, SSEEventType, SSEPayload

logger = logging.getLogger(__name__)


class StreamEmitter:
    """Buffers SSE events for a single run and exposes an async generator.

    Events are kept in an in-memory list so any number of consumers can replay
    from the beginning independently.  An asyncio.Event signals new arrivals so
    consumers wait efficiently rather than polling.
    """

    def __init__(self, run_id: str, events_file: str | None = None) -> None:
        self.run_id = run_id
        self.done = False
        self._buffer: list[SSEEvent] = []
        self._notify: asyncio.Event = asyncio.Event()
        self._seq = 0
        self._events_file = events_file
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
        self._buffer.append(event)
        self._notify.set()
        if self._events_file:
            with open(self._events_file, "a") as f:
                f.write(json.dumps(event.model_dump(mode="json")) + "\n")
        logger.debug("[%s] emit %s %s", self.run_id, event_type, message)

    def close(self) -> None:
        self.done = True
        self._notify.set()

    async def __aiter__(self) -> AsyncIterator[dict[str, str]]:
        pos = 0
        while True:
            # Yield all buffered events from current position.
            while pos < len(self._buffer):
                yield {"data": json.dumps(self._buffer[pos].model_dump(mode="json"))}
                pos += 1

            if self.done:
                return

            # Clear the notification flag, then re-check buffer and done in case
            # emit()/close() fired between our last check and this clear().
            self._notify.clear()
            while pos < len(self._buffer):
                yield {"data": json.dumps(self._buffer[pos].model_dump(mode="json"))}
                pos += 1
            if self.done:
                return

            await self._notify.wait()


class StreamRegistry:
    def __init__(self) -> None:
        self._emitters: dict[str, StreamEmitter] = {}

    def create(self, run_id: str, events_file: str | None = None) -> StreamEmitter:
        emitter = StreamEmitter(run_id, events_file=events_file)
        self._emitters[run_id] = emitter
        return emitter

    def get(self, run_id: str) -> StreamEmitter | None:
        return self._emitters.get(run_id)

    async def connect(self, run_id: str) -> StreamEmitter | None:
        """Async lookup — base class delegates to get().

        Overridden by RedisStreamRegistry to check Redis on a local-cache miss,
        enabling cross-replica SSE streaming.
        """
        return self.get(run_id)

    def remove(self, run_id: str) -> None:
        self._emitters.pop(run_id, None)


def _make_stream_registry() -> "StreamRegistry":
    if settings.state_backend == "redis":
        from agentflow.core.redis_client import get_redis
        from agentflow.orchestrator.stream_redis import RedisStreamRegistry
        return RedisStreamRegistry(get_redis(), ttl=settings.redis_key_ttl)  # type: ignore[return-value]
    return StreamRegistry()


stream_registry = _make_stream_registry()
