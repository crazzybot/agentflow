"""Orchestration engine — steps 01-07 from the design doc."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from agentflow.config import settings
from agentflow.core.bus import task_bus
from agentflow.core.context import RunContext, context_store
from agentflow.core.models import (AgentResult, AgentStatus, ExecutionPlan,
                                   SSEEventType, Subtask, TaskConstraints,
                                   TaskContext, TaskEnvelope)
from agentflow.core.registry import AgentRegistry
from agentflow.llm import LLMClient
from agentflow.orchestrator.decomposer import expand_plan
from agentflow.orchestrator.planner import create_plan
from agentflow.orchestrator.reporter import compile_report
from agentflow.orchestrator.scheduler import DependencyGraph
from agentflow.orchestrator.stream import StreamEmitter, stream_registry

logger = logging.getLogger(__name__)


def _compute_subtask_budget(
    subtask: Subtask,
    plan: ExecutionPlan,
    completed: set[str],
    failed: set[str],
    in_flight: dict[str, Any],
    ctx: RunContext,
) -> float | None:
    """Compute a concrete USD budget for *subtask* from the run's remaining budget.

    The fraction assigned by the planner is relative to the pool of all not-yet-started
    subtasks (including this one), so savings from earlier tasks automatically flow to
    later ones.  Returns None when no run budget was set or the subtask has no fraction.
    """
    if ctx.budget_usd is None or subtask.budget_fraction is None:
        return None

    remaining = ctx.remaining_budget_usd() or 0.0

    # Pending = not completed, not failed, not already in flight (they were budgeted earlier)
    pending = [
        st for st in plan.subtasks
        if st.id not in completed and st.id not in failed and st.id not in in_flight
    ]
    total_pending_fraction = sum(st.budget_fraction or 0.0 for st in pending)
    if total_pending_fraction <= 0:
        return remaining

    return remaining * (subtask.budget_fraction / total_pending_fraction)


class OrchestratorEngine:
    def __init__(self, registry: AgentRegistry) -> None:
        logger.info("Initializing OrchestratorEngine with %d agents", len(registry.all()))
        logger.info("Settings: task_max_retries=%d, task_timeout_ms=%d, planner_model=%s", settings.task_max_retries, settings.task_timeout_ms, settings.planner_model )
        self.registry = registry
        self._client = LLMClient(
            api_key=settings.anthropic_api_key,
            enable_prompt_caching=settings.enable_prompt_caching,
        )
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

    async def run(
        self,
        run_id: str,
        task: str,
        user_context: dict[str, Any],
        budget_usd: float | None = None,
    ) -> None:
        events_file = (
            f"{settings.runs_dir}/{run_id}/events.jsonl"
            if settings.capture_events
            else None
        )
        results_file = (
            f"{settings.runs_dir}/{run_id}/results.jsonl"
            if settings.capture_results
            else None
        )
        emitter = stream_registry.create(run_id, events_file=events_file)
        ctx = context_store.create(run_id, results_file=results_file, budget_usd=budget_usd, user_context=user_context)
        task_bus.create_run(run_id)

        emitter.emit(SSEEventType.run_started, message=f"Run {run_id} started")

        try:
            # Step 02: LLM planning pass
            plan = await create_plan(run_id, task, self.registry, self._client, budget_usd=budget_usd, user_context=user_context)

            # Step 02b: expand subtasks for agents that declare a decomposition_prompt
            plan = await expand_plan(plan, self.registry, self._client, emitter, task=task, user_context=user_context)

            emitter.emit(
                SSEEventType.plan_created,
                message=f"Plan: {len(plan.subtasks)} subtask(s)",
                data=plan.model_dump(mode="json"),
            )

            # Step 03-06: scheduling loop
            await self._execute_plan(run_id, plan, ctx, emitter)

            # Step 07: completion
            all_results = await ctx.all_results()
            succeeded = {tid: r.output.model_dump() for tid, r in all_results.items() if r.status == AgentStatus.success}
            partials  = {tid: r.output.model_dump() for tid, r in all_results.items() if r.status == AgentStatus.partial}
            failures  = {tid: r.error for tid, r in all_results.items() if r.status == AgentStatus.failed}

            total_cost = ctx.total_cost_usd()
            cost_summary = {
                "input_tokens": sum(r.input_tokens for r in all_results.values()),
                "output_tokens": sum(r.output_tokens for r in all_results.values()),
                "cache_creation_tokens": sum(r.cache_creation_tokens for r in all_results.values()),
                "cache_read_tokens": sum(r.cache_read_tokens for r in all_results.values()),
                "cost_usd": round(total_cost, 6),
            }

            # Compile and save the final report
            report_path = await compile_report(run_id, task, plan, all_results, self._client, cost_summary)

            if failures and not succeeded and not partials:
                emitter.emit(SSEEventType.run_error, message=f"All subtasks failed: {list(failures)}", data=failures)
            elif failures or partials:
                emitter.emit(
                    SSEEventType.run_complete,
                    message=f"Run complete — {len(succeeded)} succeeded, {len(partials)} partial, {len(failures)} failed",
                    data={"results": succeeded, "partial": partials, "failed": failures, "report": report_path, "cost": cost_summary},
                )
            else:
                emitter.emit(
                    SSEEventType.run_complete,
                    message="All subtasks complete",
                    data={"results": succeeded, "report": report_path, "cost": cost_summary},
                )

        except Exception as exc:
            logger.exception("[%s] Orchestrator error", run_id)
            emitter.emit(SSEEventType.run_error, message=str(exc))
        finally:
            self._client.stats.log_summary()
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
        failed: set[str] = set()
        in_flight: dict[str, asyncio.Task[bool]] = {}

        while len(completed) + len(failed) < len(plan.subtasks):
            ready = [
                st
                for st in graph.ready(completed, failed)
                if st.id not in in_flight
            ]

            for subtask in ready:
                task_budget = _compute_subtask_budget(subtask, plan, completed, failed, in_flight, ctx)
                in_flight[subtask.id] = asyncio.create_task(
                    self._dispatch_subtask(run_id, subtask, ctx, emitter, task_budget)
                )

            if not in_flight:
                # Nothing running and nothing dispatchable — cancel blocked remainders
                for st in plan.subtasks:
                    if st.id not in completed and st.id not in failed:
                        failed.add(st.id)
                        emitter.emit(
                            SSEEventType.task_failed,
                            agent_id=st.agent_id,
                            message=f"Subtask {st.id} cancelled: upstream dependency failed",
                            data={"subtask_id": st.id},
                        )
                break

            done, _ = await asyncio.wait(
                in_flight.values(), return_when=asyncio.FIRST_COMPLETED
            )

            for task in done:
                subtask_id = next(k for k, v in in_flight.items() if v is task)
                in_flight.pop(subtask_id)
                try:
                    succeeded: bool = task.result()
                except Exception:
                    succeeded = False
                if succeeded:
                    completed.add(subtask_id)
                else:
                    failed.add(subtask_id)

    # ------------------------------------------------------------------
    # Single subtask dispatch with retry
    # ------------------------------------------------------------------

    async def _dispatch_subtask(
        self,
        run_id: str,
        subtask: Subtask,
        ctx: RunContext,
        emitter: StreamEmitter,
        task_budget_usd: float | None = None,
    ) -> bool:
        """Dispatch a subtask with retry and continuation logic. Returns True on success/partial."""
        agent = self._agent_instances.get(subtask.agent_id)
        if agent is None:
            logger.error("[%s] No agent found for %s", run_id, subtask.agent_id)
            emitter.emit(
                SSEEventType.task_failed,
                agent_id=subtask.agent_id,
                message=f"Unknown agent: {subtask.agent_id}",
            )
            return False

        prior_results = ctx.build_prior_results(subtask.depends_on)
        prior_messages = ctx.build_prior_messages(subtask.depends_on)
        envelope = TaskEnvelope(
            parent_run_id=run_id,
            agent_id=subtask.agent_id,
            instruction=subtask.instruction,
            context=TaskContext(prior_results=prior_results, prior_messages=prior_messages, user_context=ctx.user_context),
            constraints=TaskConstraints(
                budget_usd=task_budget_usd,
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
            except asyncio.TimeoutError:
                logger.warning("[%s] Subtask %s timed out (attempt %d/%d)", run_id, subtask.id, attempt, settings.task_max_retries)
                if attempt < settings.task_max_retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                emitter.emit(
                    SSEEventType.task_failed,
                    agent_id=subtask.agent_id,
                    message=f"Subtask {subtask.id} timed out after {settings.task_max_retries} attempts",
                )
                return False

            ctx.add_result_cost(result)

            if result.status == AgentStatus.failed:
                if attempt < settings.task_max_retries:
                    logger.warning(
                        "[%s] Subtask %s failed (attempt %d/%d) — retrying in %ds",
                        run_id, subtask.id, attempt, settings.task_max_retries, 2 ** attempt,
                    )
                    await asyncio.sleep(2 ** attempt)
                    continue

                await ctx.store_result(subtask.id, result)
                fallback = self.registry.find_fallback(subtask.agent_id)
                if fallback and fallback.agent_id in self._agent_instances:
                    logger.info("[%s] Using fallback agent %s", run_id, fallback.agent_id)
                    envelope.agent_id = fallback.agent_id
                    fallback_result = await self._agent_instances[fallback.agent_id].run(envelope, emitter)
                    ctx.add_result_cost(fallback_result)
                    await ctx.store_result(subtask.id, fallback_result)
                    emitter.emit(
                        SSEEventType.task_complete,
                        agent_id=fallback.agent_id,
                        message=f"Subtask {subtask.id} complete via fallback",
                        data={"subtask_id": subtask.id},
                    )
                    return fallback_result.status == AgentStatus.success
                else:
                    emitter.emit(
                        SSEEventType.task_failed,
                        agent_id=subtask.agent_id,
                        message=f"Subtask {subtask.id} failed: {result.error}",
                        data={"subtask_id": subtask.id},
                    )
                    return False

            if result.status == AgentStatus.partial and envelope.constraints.budget_usd is None:
                # Only continue when there was no task budget — with a budget the agent
                # already ran until exhausted, so continuation would immediately stop again.
                result = await self._continue_partial(run_id, subtask, envelope, result, ctx, emitter)

            await ctx.store_result(subtask.id, result)

            if result.status == AgentStatus.partial:
                emitter.emit(
                    SSEEventType.task_partial,
                    agent_id=subtask.agent_id,
                    message=f"Subtask {subtask.id} reached its limit — output may be incomplete",
                    data={"subtask_id": subtask.id},
                )
            else:
                emitter.emit(
                    SSEEventType.task_complete,
                    agent_id=subtask.agent_id,
                    message=f"Subtask {subtask.id} complete",
                    data={"subtask_id": subtask.id},
                )
            return True

        return False

    async def _continue_partial(
        self,
        run_id: str,
        subtask: Subtask,
        envelope: TaskEnvelope,
        result: AgentResult,
        ctx: RunContext,
        emitter: StreamEmitter,
    ) -> AgentResult:
        """Attempt up to max_continuations follow-up calls when a subtask hits its iteration limit."""
        agent = self._agent_instances[subtask.agent_id]

        for cont in range(1, settings.max_continuations + 1):
            if not ctx.within_budget():
                cost = ctx.total_cost_usd()
                logger.warning(
                    "[%s] Budget $%.4f of $%.4f exhausted — accepting partial result for %s",
                    run_id, cost, ctx.budget_usd, subtask.id,
                )
                emitter.emit(
                    SSEEventType.run_budget_exceeded,
                    agent_id=subtask.agent_id,
                    message=f"Budget ${ctx.budget_usd:.2f} reached (spent ${cost:.4f}) — subtask {subtask.id} accepted as partial",
                    data={"subtask_id": subtask.id, "cost_usd": round(cost, 6), "budget_usd": ctx.budget_usd},
                )
                return result

            logger.info("[%s] Continuing subtask %s (continuation %d/%d)", run_id, subtask.id, cont, settings.max_continuations)
            emitter.emit(
                SSEEventType.task_continuing,
                agent_id=subtask.agent_id,
                message=f"Subtask {subtask.id} continuing (attempt {cont}/{settings.max_continuations})",
                data={"subtask_id": subtask.id, "continuation": cont},
            )

            try:
                next_result = await asyncio.wait_for(
                    # Fix 3: resume the existing message thread rather than rebuilding
                    # from a text summary — no re-reading, no lost tool context.
                    agent.run(envelope, emitter, resume_messages=result.messages),
                    timeout=settings.task_timeout_ms / 1000,
                )
            except asyncio.TimeoutError:
                logger.warning("[%s] Continuation %d timed out for subtask %s", run_id, cont, subtask.id)
                return result

            ctx.add_result_cost(next_result)
            result = next_result

            if result.status != AgentStatus.partial:
                return result

        return result