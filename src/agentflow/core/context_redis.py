"""Redis-backed RunContext and ContextStore.

Key layout (all with a configurable TTL):
  run:{run_id}:results        Redis hash  — subtask_id → JSON(AgentResult)
  run:{run_id}:cost           String      — cumulative USD cost (INCRBYFLOAT)
  run:{run_id}:hitl:pending   String      — "1" while awaiting human input
  run:{run_id}:hitl:queue     List        — RPUSH to deliver, BLPOP to receive

Design notes
------------
* Results are written through to Redis AND kept in a local dict so that the
  synchronous helpers build_prior_results / build_prior_messages still work
  without requiring an async call.  Cross-replica reads use get_result() /
  all_results() which go to Redis directly.

* HITL signalling uses a Redis list + a pending-flag key so that the HTTP
  route can deliver a response from any replica.  The local _is_awaiting flag
  is used for the synchronous is_awaiting_input property (status display only).

* budget_usd and user_context are kept in instance variables (set once at run
  start) rather than persisted to Redis.  They are not accessed cross-replica.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import redis.asyncio as aioredis

from agentflow.core.models import AgentResult, HumanInputResponse

logger = logging.getLogger(__name__)


class RedisRunContext:
    """Per-run context backed by Redis."""

    def __init__(
        self,
        run_id: str,
        redis: aioredis.Redis,
        results_file: str | None = None,
        budget_usd: float | None = None,
        user_context: dict | None = None,
        ttl: int = 86_400,
    ) -> None:
        self.run_id = run_id
        self._redis = redis
        self._ttl = ttl
        self.budget_usd = budget_usd
        self.user_context: dict = user_context or {}
        self._results_file = results_file
        self._lock = asyncio.Lock()
        self.human_input_lock = asyncio.Lock()
        self._local_results: dict[str, AgentResult] = {}
        self._total_cost_usd_local: float = 0.0
        self._is_awaiting: bool = False
        # Redis key names
        self._results_key = f"run:{run_id}:results"
        self._cost_key = f"run:{run_id}:cost"
        self._hitl_pending_key = f"run:{run_id}:hitl:pending"
        self._hitl_queue_key = f"run:{run_id}:hitl:queue"
        if results_file:
            os.makedirs(os.path.dirname(results_file), exist_ok=True)

    # ------------------------------------------------------------------
    # HITL
    # ------------------------------------------------------------------

    @property
    def is_awaiting_input(self) -> bool:
        return self._is_awaiting

    def request_human_input(self) -> None:
        self._is_awaiting = True
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._redis.set(self._hitl_pending_key, "1", ex=self._ttl))
        except RuntimeError:
            pass

    def provide_human_input(self, response: HumanInputResponse) -> bool:
        """Deliver a human response.  Returns False if no input was pending."""
        if not self._is_awaiting:
            return False
        self._is_awaiting = False
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._push_hitl_response(response))
        except RuntimeError:
            logger.warning("[%s] provide_human_input called outside async context", self.run_id)
        return True

    async def _push_hitl_response(self, response: HumanInputResponse) -> None:
        await self._redis.rpush(self._hitl_queue_key, response.model_dump_json())
        await self._redis.delete(self._hitl_pending_key)

    async def await_human_input(self) -> HumanInputResponse:
        """Block until a human response is pushed to the Redis queue.

        Uses 1-second BLPOP polling so asyncio.wait_for can cancel cleanly.
        """
        while True:
            result = await self._redis.blpop(self._hitl_queue_key, timeout=1)
            if result is not None:
                _, data = result
                self._is_awaiting = False
                return HumanInputResponse.model_validate_json(data)

    # ------------------------------------------------------------------
    # Budget / cost tracking  (local + Redis)
    # ------------------------------------------------------------------

    def add_result_cost(self, result: AgentResult) -> None:
        self._total_cost_usd_local += result.cost_usd
        if result.cost_usd > 0:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    self._redis.incrbyfloat(self._cost_key, result.cost_usd)
                )
            except RuntimeError:
                pass

    def total_cost_usd(self) -> float:
        return self._total_cost_usd_local

    def remaining_budget_usd(self) -> float | None:
        if self.budget_usd is None:
            return None
        return max(0.0, self.budget_usd - self._total_cost_usd_local)

    def within_budget(self) -> bool:
        if self.budget_usd is None:
            return True
        return self._total_cost_usd_local < self.budget_usd

    # ------------------------------------------------------------------
    # Result storage  (async, write-through to Redis + local cache)
    # ------------------------------------------------------------------

    async def store_result(self, subtask_id: str, result: AgentResult) -> None:
        async with self._lock:
            self._local_results[subtask_id] = result
            await self._redis.hset(self._results_key, subtask_id, result.model_dump_json())
            await self._redis.expire(self._results_key, self._ttl)
            if self._results_file:
                entry = {"subtask_id": subtask_id, **result.model_dump(mode="json")}
                with open(self._results_file, "a") as f:
                    f.write(json.dumps(entry) + "\n")

    async def get_result(self, subtask_id: str) -> AgentResult | None:
        async with self._lock:
            if subtask_id in self._local_results:
                return self._local_results[subtask_id]
            data = await self._redis.hget(self._results_key, subtask_id)
            if data is None:
                return None
            result = AgentResult.model_validate_json(data)
            self._local_results[subtask_id] = result
            return result

    async def all_results(self) -> dict[str, AgentResult]:
        async with self._lock:
            raw = await self._redis.hgetall(self._results_key)
            results = {k: AgentResult.model_validate_json(v) for k, v in raw.items()}
            self._local_results.update(results)
            return dict(results)

    # ------------------------------------------------------------------
    # Synchronous context-building helpers  (use local cache only)
    # ------------------------------------------------------------------

    def build_prior_results(self, dep_ids: list[str]) -> dict[str, Any]:
        return {
            dep_id: self._local_results[dep_id].output.text
            or str(self._local_results[dep_id].output.structured)
            for dep_id in dep_ids
            if dep_id in self._local_results
        }

    def build_prior_messages(self, dep_ids: list[str]) -> list[Any]:
        if len(dep_ids) != 1:
            return []
        dep_id = dep_ids[0]
        result = self._local_results.get(dep_id)
        if result is None or not result.messages:
            return []
        return result.messages


class RedisContextStore:
    """Global store keyed by run_id — backed by Redis."""

    def __init__(self, redis: aioredis.Redis, ttl: int = 86_400) -> None:
        self._redis = redis
        self._ttl = ttl
        self._runs: dict[str, RedisRunContext] = {}

    def create(
        self,
        run_id: str,
        results_file: str | None = None,
        budget_usd: float | None = None,
        user_context: dict | None = None,
    ) -> RedisRunContext:
        ctx = RedisRunContext(
            run_id,
            self._redis,
            results_file=results_file,
            budget_usd=budget_usd,
            user_context=user_context,
            ttl=self._ttl,
        )
        self._runs[run_id] = ctx
        return ctx

    def get(self, run_id: str) -> RedisRunContext | None:
        return self._runs.get(run_id)

    def remove(self, run_id: str) -> None:
        self._runs.pop(run_id, None)
