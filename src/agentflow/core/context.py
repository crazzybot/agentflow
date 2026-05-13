"""Per-run context store — holds subtask results and shared memory."""
from __future__ import annotations

import asyncio
from typing import Any

from agentflow.core.models import AgentResult


class RunContext:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._results: dict[str, AgentResult] = {}
        self._lock = asyncio.Lock()

    async def store_result(self, subtask_id: str, result: AgentResult) -> None:
        async with self._lock:
            self._results[subtask_id] = result

    async def get_result(self, subtask_id: str) -> AgentResult | None:
        async with self._lock:
            return self._results.get(subtask_id)

    async def all_results(self) -> dict[str, AgentResult]:
        async with self._lock:
            return dict(self._results)

    def build_prior_results(self, dep_ids: list[str]) -> dict[str, Any]:
        """Return structured outputs from completed dependencies."""
        return {
            dep_id: self._results[dep_id].output.model_dump()
            for dep_id in dep_ids
            if dep_id in self._results
        }


class ContextStore:
    """Global store keyed by run_id."""

    def __init__(self) -> None:
        self._runs: dict[str, RunContext] = {}

    def create(self, run_id: str) -> RunContext:
        ctx = RunContext(run_id)
        self._runs[run_id] = ctx
        return ctx

    def get(self, run_id: str) -> RunContext | None:
        return self._runs.get(run_id)

    def remove(self, run_id: str) -> None:
        self._runs.pop(run_id, None)


context_store = ContextStore()
