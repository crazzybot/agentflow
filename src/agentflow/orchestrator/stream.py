"""SSE stream emitter — per-run event channel consumed by the HTTP layer."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from agentflow.core.models import SSEEvent, SSEEventType, SSEPayload

logger = logging.getLogger(__name__)


class StreamEmitter:
    """Buffers SSE events for a single run and exposes an async generator."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._queue: asyncio.Queue[SSEEvent | None] = asyncio.Queue()
        self._seq = 0

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
        self._queue.put_nowait(event)
        logger.debug("[%s] emit %s %s", self.run_id, event_type, message)

    def close(self) -> None:
        self._queue.put_nowait(None)  # sentinel

    async def __aiter__(self) -> AsyncIterator[dict[str, str]]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield {"data": json.dumps(event.model_dump(mode="json"))}


class StreamRegistry:
    def __init__(self) -> None:
        self._emitters: dict[str, StreamEmitter] = {}

    def create(self, run_id: str) -> StreamEmitter:
        emitter = StreamEmitter(run_id)
        self._emitters[run_id] = emitter
        return emitter

    def get(self, run_id: str) -> StreamEmitter | None:
        return self._emitters.get(run_id)

    def remove(self, run_id: str) -> None:
        self._emitters.pop(run_id, None)


stream_registry = StreamRegistry()
