"""In-process async task bus backed by asyncio queues.

Designed so that it can be swapped for Redis Streams later without changing
the rest of the codebase — the interface stays the same.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class TaskBus:
    """Simple per-run asyncio queue pair: dispatch (in) and results (out)."""

    def __init__(self) -> None:
        self._dispatch_queues: dict[str, asyncio.Queue[Any]] = {}
        self._result_queues: dict[str, asyncio.Queue[Any]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def create_run(self, run_id: str) -> None:
        self._dispatch_queues[run_id] = asyncio.Queue()
        self._result_queues[run_id] = asyncio.Queue()
        logger.debug("Created bus channels for run %s", run_id)

    def close_run(self, run_id: str) -> None:
        self._dispatch_queues.pop(run_id, None)
        self._result_queues.pop(run_id, None)

    # ------------------------------------------------------------------
    # Dispatch (orchestrator → worker)
    # ------------------------------------------------------------------

    async def enqueue_task(self, run_id: str, envelope: Any) -> None:
        q = self._dispatch_queues[run_id]
        await q.put(envelope)

    async def dequeue_task(self, run_id: str) -> Any:
        return await self._dispatch_queues[run_id].get()

    def task_done(self, run_id: str) -> None:
        self._dispatch_queues[run_id].task_done()

    # ------------------------------------------------------------------
    # Results (worker → orchestrator)
    # ------------------------------------------------------------------

    async def publish_result(self, run_id: str, result: Any) -> None:
        await self._result_queues[run_id].put(result)

    async def consume_result(self, run_id: str) -> Any:
        return await self._result_queues[run_id].get()


task_bus = TaskBus()
