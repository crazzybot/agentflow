"""Orchestration engine — steps 01-07 from the design doc."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import anthropic

from agentflow.config import settings
from agentflow.core.bus import task_bus
from agentflow.core.context import RunContext, context_store
from agentflow.core.models import (
    AgentStatus,
    ExecutionPlan,
    SSEEventType,
    Subtask,
    TaskConstraints,
    TaskContext,
    TaskEnvelope,
)
from agentflow.core.registry import AgentRegistry
from agentflow.orchestrator.planner import create_plan
from agentflow.orchestrator.scheduler import DependencyGraph
from agentflow.orchestrator.stream import StreamEmitter, stream_registry

logger = logging.getLogger(__name__)


class OrchestratorEngine:
    def __init__(self, registry: AgentRegistry) -> None:
        self.registry = registry
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._agent_instances: dict[str, Any] = {}
        self._build_agents()

    def _build_agents(self) -> None:
        """Instantiate one generic Agent per registered manifest."""
        from agentflow.agents import Agent

        for manifest in self.registry.all():
            self._agent_instances[manifest.agent_id] = Agent(manifest, self._client)
            logger.info("Instantiated agent %s", manifest.agent_id)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self, run_id: str, task: str, user_context: dict[str, Any]) -> None:
        emitter = stream_registry.create(run_id)
        ctx = context_store.create(run_id)
        task_bus.create_run(run_id)

        emitter.emit(SSEEventType.run_started, message=f"Run {run_id} started")

        try:
            # Step 02: LLM planning pass
            plan = await create_plan(run_id, task, self.registry, self._client)
            emitter.emit(
                SSEEventType.plan_created,
                message=f"Plan: {len(plan.subtasks)} subtask(s)",
                data=plan.model_dump(mode="json"),
            )

            # Step 03-06: scheduling loop
            await self._execute_plan(run_id, plan, ctx, emitter)

            # Step 07: completion
            all_results = await ctx.all_results()
            assembled = {tid: r.output.model_dump() for tid, r in all_results.items()}
            emitter.emit(SSEEventType.run_complete, message="All subtasks complete", data=assembled)

        except Exception as exc:
            logger.exception("[%s] Orchestrator error", run_id)
            emitter.emit(SSEEventType.run_error, message=str(exc))
        finally:
            emitter.close()
            task_bus.close_run(run_id)
            context_store.remove(run_id)

    # ------------------------------------------------------------------
    # Scheduling loop
    # ------------------------------------------------------------------

    async def _execute_plan(
        self,
        run_id: str,
        plan: ExecutionPlan,
        ctx: RunContext,
        emitter: StreamEmitter,
    ) -> None:
        graph = DependencyGraph(plan)
        completed: set[str] = set()
        in_flight: dict[str, asyncio.Task[None]] = {}

        while len(completed) < len(plan.subtasks):
            ready = [
                st
                for st in graph.ready(completed)
                if st.id not in in_flight and st.id not in completed
            ]

            for subtask in ready:
                in_flight[subtask.id] = asyncio.create_task(
                    self._dispatch_subtask(run_id, subtask, ctx, emitter)
                )

            if not in_flight:
                break  # nothing to wait for — guard against planning errors

            done, _ = await asyncio.wait(
                in_flight.values(), return_when=asyncio.FIRST_COMPLETED
            )

            for task in done:
                subtask_id = next(k for k, v in in_flight.items() if v is task)
                in_flight.pop(subtask_id)
                completed.add(subtask_id)

    # ------------------------------------------------------------------
    # Single subtask dispatch with retry
    # ------------------------------------------------------------------

    async def _dispatch_subtask(
        self,
        run_id: str,
        subtask: Subtask,
        ctx: RunContext,
        emitter: StreamEmitter,
    ) -> None:
        agent = self._agent_instances.get(subtask.agent_id)
        if agent is None:
            logger.error("[%s] No agent found for %s", run_id, subtask.agent_id)
            emitter.emit(
                SSEEventType.task_failed,
                agent_id=subtask.agent_id,
                message=f"Unknown agent: {subtask.agent_id}",
            )
            return

        prior_results = ctx.build_prior_results(subtask.depends_on)
        envelope = TaskEnvelope(
            parent_run_id=run_id,
            agent_id=subtask.agent_id,
            instruction=subtask.instruction,
            context=TaskContext(prior_results=prior_results),
            constraints=TaskConstraints(
                max_tokens=settings.task_max_tokens,
                timeout_ms=settings.task_timeout_ms,
            ),
        )

        emitter.emit(
            SSEEventType.task_dispatched,
            agent_id=subtask.agent_id,
            message=f"Dispatching subtask {subtask.id} to {subtask.agent_id}",
            data={"subtask_id": subtask.id, "task_id": envelope.task_id},
        )

        for attempt in range(1, settings.task_max_retries + 1):
            try:
                result = await asyncio.wait_for(
                    agent.run(envelope, emitter),
                    timeout=settings.task_timeout_ms / 1000,
                )

                if result.status == AgentStatus.failed and attempt < settings.task_max_retries:
                    delay = 2 ** attempt
                    logger.warning(
                        "[%s] Subtask %s failed (attempt %d/%d) — retrying in %ds",
                        run_id, subtask.id, attempt, settings.task_max_retries, delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                await ctx.store_result(subtask.id, result)

                if result.status == AgentStatus.failed:
                    # Check for fallback agent
                    fallback = self.registry.find_fallback(subtask.agent_id)
                    if fallback and fallback.agent_id in self._agent_instances:
                        logger.info("[%s] Using fallback agent %s", run_id, fallback.agent_id)
                        envelope.agent_id = fallback.agent_id
                        fallback_result = await self._agent_instances[fallback.agent_id].run(envelope, emitter)
                        await ctx.store_result(subtask.id, fallback_result)
                        emitter.emit(
                            SSEEventType.task_complete,
                            agent_id=fallback.agent_id,
                            message=f"Subtask {subtask.id} complete via fallback",
                            data={"subtask_id": subtask.id},
                        )
                    else:
                        emitter.emit(
                            SSEEventType.task_failed,
                            agent_id=subtask.agent_id,
                            message=f"Subtask {subtask.id} failed: {result.error}",
                            data={"subtask_id": subtask.id},
                        )
                else:
                    emitter.emit(
                        SSEEventType.task_complete,
                        agent_id=subtask.agent_id,
                        message=f"Subtask {subtask.id} complete",
                        data={"subtask_id": subtask.id},
                    )
                return

            except asyncio.TimeoutError:
                logger.warning("[%s] Subtask %s timed out (attempt %d)", run_id, subtask.id, attempt)
                if attempt == settings.task_max_retries:
                    emitter.emit(
                        SSEEventType.task_failed,
                        agent_id=subtask.agent_id,
                        message=f"Subtask {subtask.id} timed out after {settings.task_max_retries} attempts",
                    )
