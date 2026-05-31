"""Per-run context store — holds subtask results and shared memory."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from agentflow.core.models import AgentResult


class RunContext:
    def __init__(
        self,
        run_id: str,
        results_file: str | None = None,
        budget_usd: float | None = None,
    ) -> None:
        self.run_id = run_id
        self.budget_usd = budget_usd
        self._results: dict[str, AgentResult] = {}
        self._lock = asyncio.Lock()
        self._results_file = results_file
        self._total_cost_usd: float = 0.0
        if results_file:
            os.makedirs(os.path.dirname(results_file), exist_ok=True)

    def add_result_cost(self, result: AgentResult) -> None:
        self._total_cost_usd += result.cost_usd

    def total_cost_usd(self) -> float:
        return self._total_cost_usd

    def within_budget(self) -> bool:
        """True if no budget is set, or if cost so far is below the limit."""
        if self.budget_usd is None:
            return True
        return self._total_cost_usd < self.budget_usd

    async def store_result(self, subtask_id: str, result: AgentResult) -> None:
        async with self._lock:
            self._results[subtask_id] = result
            if self._results_file:
                entry = {"subtask_id": subtask_id, **result.model_dump(mode="json")}
                with open(self._results_file, "a") as f:
                    f.write(json.dumps(entry) + "\n")

    async def get_result(self, subtask_id: str) -> AgentResult | None:
        async with self._lock:
            return self._results.get(subtask_id)

    async def all_results(self) -> dict[str, AgentResult]:
        async with self._lock:
            return dict(self._results)

    def build_prior_results(self, dep_ids: list[str]) -> dict[str, Any]:
        """Return text output from completed dependencies.

        Sends only the text representation to avoid duplicating data: AgentOutput
        stores both `text` (raw string) and `structured` (json.loads of that same
        string), so model_dump() would transmit the same payload twice.
        """
        return {
            dep_id: self._results[dep_id].output.text
            or str(self._results[dep_id].output.structured)
            for dep_id in dep_ids
            if dep_id in self._results
        }

    def build_prior_messages(self, dep_ids: list[str]) -> list[Any]:
        """Return the full conversation messages from the single upstream dependency.

        Only populated when there is exactly one dependency that has stored messages;
        for multiple dependencies we fall back to text context (build_prior_results)
        because merging separate conversation threads is not well-defined.
        """
        if len(dep_ids) != 1:
            return []
        dep_id = dep_ids[0]
        result = self._results.get(dep_id)
        if result is None or not result.messages:
            return []
        return result.messages


class ContextStore:
    """Global store keyed by run_id."""

    def __init__(self) -> None:
        self._runs: dict[str, RunContext] = {}

    def create(
        self,
        run_id: str,
        results_file: str | None = None,
        budget_usd: float | None = None,
    ) -> RunContext:
        ctx = RunContext(run_id, results_file=results_file, budget_usd=budget_usd)
        self._runs[run_id] = ctx
        return ctx

    def get(self, run_id: str) -> RunContext | None:
        return self._runs.get(run_id)

    def remove(self, run_id: str) -> None:
        self._runs.pop(run_id, None)


context_store = ContextStore()
