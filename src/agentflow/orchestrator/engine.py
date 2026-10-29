"""Orchestration engine — steps 01-07 from the design doc."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentflow.config import settings
from agentflow.core.bus import task_bus
from agentflow.core.context import RunContext, context_store
from agentflow.core.models import (AgentResult, AgentStatus, ExecutionPlan,
                                   HumanInputRequest, HumanInputResponse,
                                   RunMeta, SSEEvent, SSEEventType, SSEPayload,
                                   Subtask, TaskConstraints, TaskContext, TaskEnvelope)
from agentflow.core.registry import AgentRegistry
from agentflow.core.session import (RunSession, SessionEventType, _current_session,
                                     read_session, record, replay, result_data,
                                     session_path)
from agentflow.core.workspace import (WorkspaceLocks, _current_workspace,
                                       run_workspace_rel, workspace_path)
from agentflow.llm import LLMClient
from agentflow.orchestrator.decomposer import decompose_subtask
from agentflow.orchestrator.planner import create_plan
from agentflow.orchestrator.reporter import compile_report
from agentflow.orchestrator.resume import ResumeState, load_resumable
from agentflow.orchestrator.scheduler import DependencyGraph
from agentflow.orchestrator.stream import StreamEmitter, stream_registry
from agentflow.tools.artifact_tracker import ArtifactSink, _current_sink
from agentflow.tools.kb_dispatcher import _kb_dispatch_fn

logger = logging.getLogger(__name__)

# Keys that belong in the planner's context only.  The planner embeds whatever
# each agent needs into its subtask instruction; agents never need the raw blobs.
_PLANNER_ONLY_CONTEXT_KEYS: frozenset[str] = frozenset(
    {"prior_report", "prior_subtask_outputs", "prior_run_id", "prior_task"}
)


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


_INTERRUPTED_REPORT_HEADER = "# Run Interrupted"


class RunAlreadyActiveError(Exception):
    """Raised when resuming a run that is still executing in this process."""


def _remove_interrupted_tombstone(run_id: str) -> None:
    """Drop the placeholder report reconciliation wrote, so a resumed run reads as
    in progress again. A real report (the run died after compiling it) is kept."""
    report_file = Path(settings.runs_dir) / run_id / "report.md"
    if report_file.exists() and report_file.read_text(encoding="utf-8").startswith(_INTERRUPTED_REPORT_HEADER):
        report_file.unlink()


async def _record_finished(subtask_id: str, *, ok: bool, reason: str | None = None) -> None:
    """Log the scheduler's completed/failed decision for a subtask."""
    data: dict[str, Any] = {"ok": ok}
    if reason is not None:
        data["reason"] = reason
    await record(SessionEventType.subtask_finished, subtask_id=subtask_id, data=data)


async def _store_result(ctx: RunContext, subtask_id: str, result: AgentResult) -> None:
    """Store a subtask result in the run context and the session log."""
    await ctx.store_result(subtask_id, result)
    await record(
        SessionEventType.result_stored,
        agent_id=result.agent_id,
        subtask_id=subtask_id,
        data=result_data(result),
    )


async def _add_cost(ctx: RunContext, result: AgentResult) -> None:
    """Charge a result's cost to the run budget and the session log."""
    ctx.add_result_cost(result)
    await record(
        SessionEventType.cost_added,
        agent_id=result.agent_id,
        data={"cost_usd": result.cost_usd, "task_id": result.task_id},
    )


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
        self._run_tasks: dict[str, asyncio.Task] = {}
        self._workspace_locks = WorkspaceLocks()
        # Runs being resumed by wake(): consulted by the scheduler and dispatch.
        self._resume: dict[str, ResumeState] = {}
        self._build_agents()

    def cancel_run(self, run_id: str) -> bool:
        """Cancel an active run. Returns True if the run was found and cancelled."""
        task = self._run_tasks.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def reconcile_orphaned_runs(self) -> list[str]:
        """Mark runs left in flight by a previous process as interrupted (startup).

        A run with a session log is orphaned when the log has no terminal event; it
        gets a non-terminal ``run_interrupted`` event and stays resumable via
        ``wake()``. A run recorded before session logs existed is orphaned when it
        has no report.md. Either way a ``run:error`` SSE event and a tombstone
        report are written so the run does not look perpetually in progress — but
        an existing report is never overwritten (a run can die after compiling it).
        Returns the ids of the runs reconciled.
        """
        runs_dir = Path(settings.runs_dir)
        if not runs_dir.exists():
            return []
        reconciled: list[str] = []
        for run_dir in runs_dir.iterdir():
            if not run_dir.is_dir() or not (run_dir / "meta.json").exists():
                continue
            run_id = run_dir.name
            report_file = run_dir / "report.md"

            resumable = session_path(run_id).exists()
            if resumable:
                snap = replay(run_id, read_session(run_id))
                # Finished, or already reconciled by an earlier restart.
                if not snap.resumable or snap.interrupted or snap.status == "interrupted":
                    continue
                RunSession(run_id).append_sync(
                    SessionEventType.run_interrupted, data={"reason": "service restarted"}
                )
            elif report_file.exists():
                continue

            events_file = run_dir / "events.jsonl"
            if events_file.exists():
                lines = [line for line in events_file.read_text(encoding="utf-8").splitlines() if line.strip()]
                event = SSEEvent(
                    run_id=run_id,
                    seq=len(lines),
                    type=SSEEventType.run_error,
                    payload=SSEPayload(message="Run interrupted: service restarted"),
                )
                with events_file.open("a", encoding="utf-8") as f:
                    f.write(event.model_dump_json() + "\n")

            if not report_file.exists():
                hint = (
                    f"\nIt can be resumed with `POST /api/runs/{run_id}/resume`.\n"
                    if resumable else ""
                )
                report_file.write_text(
                    f"{_INTERRUPTED_REPORT_HEADER}\n\n"
                    "This run did not complete — the service restarted while it was in progress.\n"
                    + hint,
                    encoding="utf-8",
                )
            logger.warning("Reconciled orphaned run %s%s", run_id, " (resumable)" if resumable else "")
            reconciled.append(run_id)
        return reconciled

    def schedule_resume(self, run_id: str) -> asyncio.Task[None]:
        """Validate that *run_id* can be resumed here, then start ``wake()`` in the background.

        Raises RunNotResumableError (no log, or already finished) or
        RunAlreadyActiveError (still executing in this process).
        """
        if self.is_active(run_id):
            raise RunAlreadyActiveError(f"Run {run_id!r} is already running")
        load_resumable(run_id)
        task = asyncio.create_task(self.wake(run_id))
        # Register now, not when the task first runs: a second resume request
        # arriving before then must already see the run as active.
        self._run_tasks[run_id] = task
        return task

    def _resumed_outcomes(self, run_id: str, ids: list[str]) -> tuple[set[str], set[str]]:
        """Completed/failed sets to seed a scheduler with when resuming a run."""
        resume = self._resume.get(run_id)
        if resume is None:
            return set(), set()
        completed = {i for i in ids if resume.finished.get(i) is True}
        failed = {i for i in ids if resume.finished.get(i) is False}
        return completed, failed

    def _build_agents(self) -> None:
        """Instantiate one generic Agent per registered manifest."""
        from agentflow.agents import Agent

        for manifest in self.registry.all():
            self._agent_instances[manifest.agent_id] = Agent(manifest, self._client)
            logger.info("Instantiated agent %s", manifest.agent_id)

    async def _generate_run_name(self, task: str) -> str:
        try:
            response = await self._client.messages.create(
                model=settings.reporter_model,
                max_tokens=24,
                messages=[{
                    "role": "user",
                    "content": (
                        f'Give this task a short name: 3-5 words, title case, no punctuation.\n'
                        f'Task: "{task[:300]}"\n'
                        f'Reply with only the name.'
                    ),
                }],
            )
            for block in response.content:
                if hasattr(block, "text") and block.text.strip():
                    return block.text.strip()
        except Exception:
            logger.warning("Failed to generate run name", exc_info=True)
        return " ".join(task.split()[:5])

    def _write_meta(self, run_id: str, task: str, name: str, created_at: str, workspace: str) -> None:
        meta = RunMeta(run_id=run_id, task=task, name=name, created_at=created_at, workspace=workspace)
        meta_path = Path(settings.runs_dir) / run_id / "meta.json"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(meta.model_dump_json(indent=2))

    # ------------------------------------------------------------------
    # Mode routing helpers
    # ------------------------------------------------------------------

    async def _is_single_agent_task(self, task: str) -> bool:
        """Cheap Haiku call to decide if the task can be handled by a single agent.

        Returns True when a single capable agent with tools can handle the task
        end-to-end.  Falls back to False (multi-agent plan) on any error so the
        known-good path is always the safe default.
        """
        try:
            response = await self._client.messages.create(
                model=settings.reporter_model,
                max_tokens=32,
                system=(
                    "Classify this AI task. Reply with ONLY valid JSON: "
                    '{"route":"plan"} or {"route":"direct"}.\n'
                    'Route "plan": task has multiple clearly independent parallel workstreams '
                    "that genuinely benefit from specialist agents running simultaneously.\n"
                    'Route "direct": a single capable agent with tools can handle it end-to-end.\n'
                    'Default to "direct" when unsure.'
                ),
                messages=[{"role": "user", "content": task[:2000]}],
            )
            for block in response.content:
                if hasattr(block, "text"):
                    data = json.loads(block.text.strip())
                    return data.get("route") == "direct"
        except Exception:
            logger.warning("[auto] Task classification failed — defaulting to plan mode", exc_info=True)
        return False

    def _make_direct_plan(self, run_id: str, task: str) -> ExecutionPlan:
        """Build a synthetic single-subtask plan that bypasses the LLM planner."""
        agent_id = settings.direct_agent_id
        if not agent_id:
            raise RuntimeError(
                "DIRECT_AGENT_ID is not configured. "
                "Set it in .env to the agent_id to use for direct/auto mode."
            )
        if agent_id not in self._agent_instances:
            raise RuntimeError(
                f"direct_agent_id {agent_id!r} is not a registered agent. "
                f"Available agents: {sorted(self._agent_instances)}"
            )
        return ExecutionPlan(
            run_id=run_id,
            subtasks=[Subtask(id="direct-task", agent_id=agent_id, instruction=task, depends_on=[])],
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        run_id: str,
        task: str,
        user_context: dict[str, Any],
        budget_usd: float | None = None,
        workspace: str | None = None,
    ) -> None:
        """Execute one run end to end.

        *workspace* is the run's workspace path relative to ``settings.workspace_dir``:
        a named workspace, or a prior run's recorded path when following up. ``None``
        gives the run a fresh ``runs/<run_id>`` workspace of its own.
        """
        await self._run(run_id, task, user_context, budget_usd, workspace, resume=None)

    def is_active(self, run_id: str) -> bool:
        task = self._run_tasks.get(run_id)
        return task is not None and not task.done()

    async def wake(self, run_id: str) -> None:
        """Resume an interrupted run from its session log.

        Finished subtasks are not re-run, the logged plan is reused, and subtasks
        that were mid-flight continue from their last logged agent turn. Raises
        RunNotResumableError when the log is missing or the run already finished.
        """
        snap = load_resumable(run_id)
        resume = ResumeState.from_snapshot(snap)
        logger.info(
            "[%s] Resuming run: %d subtask(s) finished, %d with a transcript to continue",
            run_id, len(resume.finished), len(resume.transcripts),
        )
        await self._run(
            run_id, snap.task, snap.user_context, snap.budget_usd, snap.workspace, resume=resume
        )

    async def _run(
        self,
        run_id: str,
        task: str,
        user_context: dict[str, Any],
        budget_usd: float | None,
        workspace: str | None,
        resume: ResumeState | None,
    ) -> None:
        # Resolve before any per-run state exists: an unsafe path fails loudly here
        # instead of leaving an emitter open that nothing will ever close.
        ws_rel = workspace if workspace is not None else run_workspace_rel(run_id, None)
        ws_dir = workspace_path(ws_rel)

        current_task = asyncio.current_task()
        if current_task is not None:
            self._run_tasks[run_id] = current_task

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
        ctx = context_store.create(run_id, results_file=results_file, budget_usd=budget_usd, user_context=user_context, task=task)
        task_bus.create_run(run_id)
        if resume is not None:
            await ctx.restore(resume.results, resume.total_cost_usd)
            self._resume[run_id] = resume

        created_at = datetime.now(timezone.utc).isoformat()
        # Both are independent cheap Haiku calls that only need `task` — run them
        # concurrently (and alongside the run setup below) instead of stacking
        # their latency sequentially before any real work starts. A resumed run
        # already has its name in meta.json.
        name_task = (
            asyncio.create_task(self._generate_run_name(task)) if resume is None else None
        )
        # Direct mode is opt-in: without DIRECT_AGENT_ID there is no agent to route a
        # "direct" verdict to, so skip the classifier call and always plan. A resumed
        # run with a logged plan needs no routing decision at all.
        needs_plan = resume is None or resume.plan is None
        is_direct_task = (
            asyncio.create_task(self._is_single_agent_task(task))
            if settings.direct_agent_id and needs_plan
            else None
        )

        ws_token = _current_workspace.set(ws_dir)
        session = RunSession(run_id)  # reopens (and keeps numbering) when resuming
        session_token = _current_session.set(session)
        if resume is None:
            await record(
                SessionEventType.run_started,
                data={
                    "task": task,
                    "user_context": user_context,
                    "budget_usd": budget_usd,
                    "workspace": ws_rel,
                },
            )
        else:
            await record(
                SessionEventType.run_resumed,
                data={
                    "after_seq": resume.after_seq,
                    "finished": resume.finished,
                    "resuming": sorted(resume.transcripts),
                },
            )
            _remove_interrupted_tombstone(run_id)
        # Set by every exit path below; recorded once in `finally`.
        final_status = "error"

        if name_task is not None:
            name = await name_task
            self._write_meta(run_id, task, name, created_at, ws_rel)

        emitter.emit(
            SSEEventType.run_started,
            message=f"Run {run_id} {'resumed' if resume is not None else 'started'}",
            data={"workspace": ws_rel, "resumed": resume is not None},
        )

        artifacts_file = Path(settings.runs_dir) / run_id / "artifacts.jsonl"
        sink = ArtifactSink(artifacts_file)
        sink_token = _current_sink.set(sink)

        kb_token = None
        if "KnowledgebaseAgent" in self._agent_instances:
            async def _dispatch_to_kb(instruction: str) -> str:
                subtask = Subtask(
                    id=f"kb-{uuid.uuid4().hex[:8]}",
                    agent_id="KnowledgebaseAgent",
                    instruction=instruction,
                    depends_on=[],
                )
                ok = await self._dispatch_subtask(run_id, subtask, ctx, emitter, task_budget_usd=None)
                if ok:
                    result = await ctx.get_result(subtask.id)
                    return result.output.text if result and result.output.text else "KB ingest completed"
                return "KB ingest task failed"

            kb_token = _kb_dispatch_fn.set(_dispatch_to_kb)

        ws_stack = AsyncExitStack()
        try:
            # Runs sharing a workspace (same named project, or followups of one
            # run) are serialised so they never write the same files concurrently.
            if self._workspace_locks.is_busy(ws_rel):
                emitter.emit(
                    SSEEventType.agent_progress,
                    message=f"Waiting for workspace {ws_rel!r} — another run is using it",
                    data={"workspace": ws_rel, "waiting": True},
                )
            await ws_stack.enter_async_context(self._workspace_locks.hold(ws_rel))

            # Step 02: classify then plan.  When direct mode is configured, a cheap
            # Haiku call decides whether the task warrants multi-agent planning or
            # can be routed directly to a single agent.  Falls back to the full
            # planner on any error.
            if resume is not None and resume.plan is not None:
                plan = resume.plan
                emitter.emit(
                    SSEEventType.plan_created,
                    message=(
                        f"Resumed plan: {len(plan.subtasks)} subtask(s), "
                        f"{sum(1 for st in plan.subtasks if st.id in resume.finished)} already finished"
                    ),
                    data=plan.model_dump(mode="json"),
                )
            elif is_direct_task is not None and await is_direct_task:
                logger.info("[%s] Auto-classified as single-agent task — skipping planner", run_id)
                plan = self._make_direct_plan(run_id, task)
                await record(
                    SessionEventType.plan_created,
                    data={"plan": plan.model_dump(mode="json"), "direct": True},
                )
                emitter.emit(
                    SSEEventType.plan_created,
                    message="Single-agent mode: planner skipped (auto-classified)",
                    data=plan.model_dump(mode="json"),
                )
            else:
                plan = await create_plan(run_id, task, self.registry, self._client, emitter, budget_usd=budget_usd, user_context=user_context)
                await record(
                    SessionEventType.plan_created,
                    data={"plan": plan.model_dump(mode="json"), "direct": False},
                )
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

            final_status = "complete"
            if failures and not succeeded and not partials:
                final_status = "error"
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

        except asyncio.CancelledError:
            logger.info("[%s] Run cancelled by user", run_id)
            final_status = "cancelled"
            emitter.emit(SSEEventType.run_cancelled, message="Run cancelled")
        except Exception as exc:
            logger.exception("[%s] Orchestrator error", run_id)
            emitter.emit(SSEEventType.run_error, message=str(exc))
        finally:
            await ws_stack.aclose()
            try:
                await session.append(SessionEventType.run_finished, data={"status": final_status})
            except OSError:
                # Never let a failed log write skip the cleanup below (open emitter,
                # leaked state); the log then simply ends without a terminal event.
                logger.exception("[%s] Could not record run_finished in the session log", run_id)
            _current_session.reset(session_token)
            _current_workspace.reset(ws_token)
            self._resume.pop(run_id, None)
            self._run_tasks.pop(run_id, None)
            if kb_token is not None:
                _kb_dispatch_fn.reset(kb_token)
            _current_sink.reset(sink_token)
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
        completed, failed = self._resumed_outcomes(run_id, [st.id for st in plan.subtasks])
        in_flight: dict[str, asyncio.Task[bool]] = {}

        try:
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
                            await _record_finished(st.id, ok=False, reason="upstream_failed")
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
                    await _record_finished(subtask_id, ok=succeeded)

        except asyncio.CancelledError:
            for task in in_flight.values():
                task.cancel()
            raise

    # ------------------------------------------------------------------
    # Single subtask dispatch with retry
    # ------------------------------------------------------------------

    async def _run_micro_subtasks(
        self,
        run_id: str,
        parent: Subtask,
        micro: list[Subtask],
        ctx: RunContext,
        emitter: StreamEmitter,
        task_budget_usd: float | None,
    ) -> bool:
        """Run decomposed micro-subtasks with DAG scheduling and promote the sink result.

        Uses the same DependencyGraph scheduler as the top-level plan so parallel
        branches within a decomposed subtask run concurrently.  The sink micro-task
        (the one no other micro-task depends on) is promoted to the parent subtask ID
        so downstream subtasks can find the aggregated result.
        """
        n = len(micro)
        micro_budget = (task_budget_usd / n) if task_budget_usd else None

        micro_plan = ExecutionPlan(run_id=run_id, subtasks=micro)
        graph = DependencyGraph(micro_plan)
        completed, failed = self._resumed_outcomes(run_id, [ms.id for ms in micro])
        in_flight: dict[str, asyncio.Task[bool]] = {}

        try:
            while len(completed) + len(failed) < n:
                ready = [
                    ms for ms in graph.ready(completed, failed)
                    if ms.id not in in_flight
                ]
                for ms in ready:
                    in_flight[ms.id] = asyncio.create_task(
                        self._dispatch_subtask(run_id, ms, ctx, emitter, micro_budget, _skip_decompose=True)
                    )

                if not in_flight:
                    for ms in micro:
                        if ms.id not in completed and ms.id not in failed:
                            failed.add(ms.id)
                            await _record_finished(ms.id, ok=False, reason="upstream_failed")
                    break

                done, _ = await asyncio.wait(in_flight.values(), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    micro_id = next(k for k, v in in_flight.items() if v is task)
                    in_flight.pop(micro_id)
                    try:
                        succeeded: bool = task.result()
                    except Exception:
                        succeeded = False
                    if succeeded:
                        completed.add(micro_id)
                    else:
                        failed.add(micro_id)
                    await _record_finished(micro_id, ok=succeeded)
        except asyncio.CancelledError:
            for task in in_flight.values():
                task.cancel()
            raise

        if failed:
            emitter.emit(
                SSEEventType.task_failed,
                agent_id=parent.agent_id,
                message=f"Subtask {parent.id} failed at micro-step(s): {sorted(failed)}",
                data={"subtask_id": parent.id},
            )
            return False

        # Promote the sink micro-task's result (the one no other micro-task depends on)
        # so downstream subtasks that depend on parent.id find the aggregated output.
        all_dep_ids = {dep for ms in micro for dep in ms.depends_on}
        sinks = [ms for ms in micro if ms.id not in all_dep_ids]
        sink = sinks[-1] if sinks else micro[-1]

        all_results = await ctx.all_results()
        sink_result = all_results.get(sink.id)
        if sink_result is not None:
            await _store_result(ctx, parent.id, sink_result)

        emitter.emit(
            SSEEventType.task_complete,
            agent_id=parent.agent_id,
            message=f"Subtask {parent.id} complete ({n} micro-steps)",
            data={"subtask_id": parent.id},
        )
        return True

    async def _dispatch_subtask(
        self,
        run_id: str,
        subtask: Subtask,
        ctx: RunContext,
        emitter: StreamEmitter,
        task_budget_usd: float | None = None,
        _skip_decompose: bool = False,
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

        # Compute agent_user_context early — needed both for the decomposer and
        # for the TaskEnvelope further below.
        agent_user_context = {
            k: v for k, v in ctx.user_context.items()
            if k not in _PLANNER_ONLY_CONTEXT_KEYS
        }

        # Lazy decomposition: run at dispatch time so the decomposer sees the
        # workspace after all upstream dependencies have completed.
        # _skip_decompose is set when we are already running a micro-subtask to
        # prevent infinite recursion.
        # Resuming an interrupted run: a subtask decomposed before the crash re-runs
        # its logged micro-plan, and one that was mid-flight continues its last
        # logged transcript (which already carries any decomposer context).
        resume = self._resume.get(run_id)
        resume_messages = resume.take_transcript(subtask.id, subtask.agent_id) if resume else None
        logged_micro = resume.micro_plans.get(subtask.id) if resume and not _skip_decompose else None
        if logged_micro:
            return await self._run_micro_subtasks(
                run_id, subtask, logged_micro, ctx, emitter, task_budget_usd
            )

        if not _skip_decompose and settings.enable_decomposer and resume_messages is None:
            manifest = self.registry.get(subtask.agent_id)
            if manifest and manifest.decomposition_prompt:
                micro, decomp_ctx = await decompose_subtask(
                    subtask, manifest, run_id, self._client, emitter,
                    task=ctx.task,
                    user_context=agent_user_context,
                )
                if len(micro) > 1:
                    logger.info(
                        "[%s] Decomposed %s → %d micro-subtasks: %s",
                        run_id, subtask.id, len(micro), [m.id for m in micro],
                    )
                    if decomp_ctx:
                        # Prepend the decomposer's synthesised workspace summary to
                        # each micro-task instruction so agents don't re-read design
                        # documents independently.
                        ctx_block = f"<workspace_context>\n{decomp_ctx}\n</workspace_context>\n\n"
                        micro = [
                            ms.model_copy(update={"instruction": ctx_block + ms.instruction})
                            for ms in micro
                        ]
                    # Logged as dispatched (context included) so a resume replays it exactly.
                    await record(
                        SessionEventType.subtask_decomposed,
                        agent_id=subtask.agent_id,
                        subtask_id=subtask.id,
                        data={"micro": [m.model_dump(mode="json") for m in micro]},
                    )
                    return await self._run_micro_subtasks(
                        run_id, subtask, micro, ctx, emitter, task_budget_usd
                    )
                # Single-task path: still inject context so the agent skips re-reading.
                if decomp_ctx:
                    ctx_block = f"<workspace_context>\n{decomp_ctx}\n</workspace_context>\n\n"
                    subtask = subtask.model_copy(update={"instruction": ctx_block + subtask.instruction})

        prior_results = ctx.build_prior_results(subtask.depends_on)
        upstream_artifacts = ctx.build_upstream_artifacts(subtask.depends_on)
        envelope = TaskEnvelope(
            parent_run_id=run_id,
            agent_id=subtask.agent_id,
            instruction=subtask.instruction,
            context=TaskContext(prior_results=prior_results, upstream_artifacts=upstream_artifacts, user_context=agent_user_context),
            constraints=TaskConstraints(
                budget_usd=task_budget_usd,
                timeout_ms=settings.task_timeout_ms,
                model_tier=subtask.model_tier,
                thinking_effort=subtask.thinking_effort,
            ),
        )

        await record(
            SessionEventType.subtask_dispatched,
            agent_id=subtask.agent_id,
            subtask_id=subtask.id,
            data={
                "subtask": subtask.model_dump(mode="json"),
                "task_id": envelope.task_id,
                "budget_usd": task_budget_usd,
            },
        )
        emitter.emit(
            SSEEventType.task_dispatched,
            agent_id=subtask.agent_id,
            message=f"Dispatching subtask {subtask.id} to {subtask.agent_id}",
            data={"subtask_id": subtask.id, "task_id": envelope.task_id},
        )

        # Track which agent_id is currently registered so the finally block can
        # deregister the right one.  Set to None when we explicitly deregister
        # before switching to a fallback agent.
        registered: str | None = subtask.agent_id
        await ctx.register_agent(subtask.agent_id)
        try:
            for attempt in range(1, settings.task_max_retries + 1):
                # Only the first attempt continues the transcript; retries start over.
                attempt_resume, resume_messages = resume_messages, None
                if attempt_resume is not None:
                    emitter.emit(
                        SSEEventType.task_continuing,
                        agent_id=subtask.agent_id,
                        message=f"Subtask {subtask.id} resuming from {len(attempt_resume)} logged message(s)",
                        data={"subtask_id": subtask.id, "resumed": True},
                    )
                try:
                    result = await asyncio.wait_for(
                        agent.run(envelope, emitter, resume_messages=attempt_resume, ctx=ctx),
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

                await _add_cost(ctx, result)

                if result.status == AgentStatus.failed:
                    if attempt < settings.task_max_retries:
                        logger.warning(
                            "[%s] Subtask %s failed (attempt %d/%d) — retrying in %ds",
                            run_id, subtask.id, attempt, settings.task_max_retries, 2 ** attempt,
                        )
                        await asyncio.sleep(2 ** attempt)
                        continue

                    await _store_result(ctx, subtask.id, result)
                    fallback = self.registry.find_fallback(subtask.agent_id)
                    if fallback and fallback.agent_id in self._agent_instances:
                        logger.info("[%s] Using fallback agent %s", run_id, fallback.agent_id)
                        envelope.agent_id = fallback.agent_id
                        # Swap message routing from primary to fallback agent.
                        await ctx.deregister_agent(subtask.agent_id)
                        registered = None
                        await ctx.register_agent(fallback.agent_id)
                        try:
                            fallback_result = await self._agent_instances[fallback.agent_id].run(envelope, emitter, ctx=ctx)
                        finally:
                            await ctx.deregister_agent(fallback.agent_id)
                        await _add_cost(ctx, fallback_result)
                        await _store_result(ctx, subtask.id, fallback_result)
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

                if result.status == AgentStatus.partial:
                    if envelope.constraints.budget_usd is None:
                        result = await self._continue_partial(run_id, subtask, envelope, result, ctx, emitter)
                    else:
                        budget_remaining = envelope.constraints.budget_usd - result.cost_usd
                        # max_tokens: the budget slice didn't leave enough room for
                        # output tokens — continuing with the same budget will hit
                        # the same wall, so ask the human to increase it.
                        # Near-zero USD: same — genuinely exhausted.
                        # Iteration limit with ample budget: auto-continue, no HITL.
                        if result.hit_max_tokens or budget_remaining <= settings.agent_min_iteration_budget_usd:
                            result = await self._handle_task_budget_exhausted(run_id, subtask, envelope, result, ctx, emitter)
                        else:
                            result = await self._continue_partial(run_id, subtask, envelope, result, ctx, emitter)

                await _store_result(ctx, subtask.id, result)

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
        finally:
            if registered is not None:
                await ctx.deregister_agent(registered)

    async def _request_budget_increase(
        self,
        run_id: str,
        subtask: Subtask,
        envelope: TaskEnvelope,
        ctx: RunContext,
        emitter: StreamEmitter,
        request_type: str,
    ) -> HumanInputResponse:
        """Emit run:awaiting_input, pause the run, and return the human's decision.

        Must be called while holding ctx.human_input_lock so concurrent subtasks
        are serialised and only one question reaches the user at a time.
        """
        cost = ctx.total_cost_usd()
        if request_type == "task_budget_exhausted":
            message = (
                f"Agent '{subtask.agent_id}' used up its budget slice "
                f"(${envelope.constraints.budget_usd:.4f}). "
                f"Total run cost so far: ${cost:.4f}. "
                f"Increase the budget to continue this task?"
            )
        else:
            message = (
                f"Total run budget ${ctx.budget_usd:.2f} reached "
                f"(spent ${cost:.4f}). "
                f"Subtask '{subtask.id}' is incomplete. "
                f"Increase the budget to continue?"
            )

        request = HumanInputRequest(
            request_type=request_type,
            message=message,
            context={
                "subtask_id": subtask.id,
                "agent_id": subtask.agent_id,
                "cost_usd": round(cost, 6),
                "budget_usd": ctx.budget_usd,
                "task_budget_usd": envelope.constraints.budget_usd,
            },
        )
        emitter.emit(
            SSEEventType.run_awaiting_input,
            agent_id=subtask.agent_id,
            message=message,
            data=request.model_dump(),
        )

        await record(
            SessionEventType.human_input_requested,
            agent_id=subtask.agent_id,
            subtask_id=subtask.id,
            data={"request": request.model_dump(mode="json")},
        )
        ctx.request_human_input()
        try:
            response = await asyncio.wait_for(
                ctx.await_human_input(),
                timeout=settings.human_input_timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning("[%s] Human input timed out for subtask %s — accepting partial", run_id, subtask.id)
            response = HumanInputResponse(action="cancel")

        if response.action == "continue" and response.budget_increase_usd:
            ctx.budget_usd = (ctx.budget_usd or 0.0) + response.budget_increase_usd
            logger.info(
                "[%s] Budget increased by $%.4f → new total $%.4f",
                run_id, response.budget_increase_usd, ctx.budget_usd,
            )

        await record(
            SessionEventType.human_input_received,
            agent_id=subtask.agent_id,
            subtask_id=subtask.id,
            data={"response": response.model_dump(mode="json"), "budget_usd": ctx.budget_usd},
        )
        return response

    async def _handle_task_budget_exhausted(
        self,
        run_id: str,
        subtask: Subtask,
        envelope: TaskEnvelope,
        result: AgentResult,
        ctx: RunContext,
        emitter: StreamEmitter,
    ) -> AgentResult:
        """Handle the case where a subtask's per-task budget slice was exhausted."""
        async with ctx.human_input_lock:
            response = await self._request_budget_increase(
                run_id, subtask, envelope, ctx, emitter,
                request_type="task_budget_exhausted",
            )

        if response.action != "continue" or not response.budget_increase_usd:
            return result

        # Give the agent the newly approved budget for its continuation.
        envelope.constraints.budget_usd = response.budget_increase_usd
        return await self._continue_partial(run_id, subtask, envelope, result, ctx, emitter)

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
                async with ctx.human_input_lock:
                    # Re-check after acquiring the lock: a concurrent subtask's request
                    # may have already secured a budget increase.
                    if ctx.within_budget():
                        pass  # fall through to continue
                    else:
                        response = await self._request_budget_increase(
                            run_id, subtask, envelope, ctx, emitter,
                            request_type="run_budget_exhausted",
                        )
                        if response.action != "continue" or not response.budget_increase_usd:
                            cost = ctx.total_cost_usd()
                            logger.warning(
                                "[%s] Budget $%.4f of $%.4f exhausted — accepting partial for %s",
                                run_id, cost, ctx.budget_usd, subtask.id,
                            )
                            emitter.emit(
                                SSEEventType.run_budget_exceeded,
                                agent_id=subtask.agent_id,
                                message=f"Budget ${ctx.budget_usd:.2f} reached (spent ${cost:.4f}) — subtask {subtask.id} accepted as partial",
                                data={"subtask_id": subtask.id, "cost_usd": round(cost, 6), "budget_usd": ctx.budget_usd},
                            )
                            return result
                        # Budget increased — give agent the new allocation if task-budgeted.
                        if envelope.constraints.budget_usd is not None:
                            envelope.constraints.budget_usd = response.budget_increase_usd

            logger.info("[%s] Continuing subtask %s (continuation %d/%d)", run_id, subtask.id, cont, settings.max_continuations)
            emitter.emit(
                SSEEventType.task_continuing,
                agent_id=subtask.agent_id,
                message=f"Subtask {subtask.id} continuing (attempt {cont}/{settings.max_continuations})",
                data={"subtask_id": subtask.id, "continuation": cont},
            )

            try:
                next_result = await asyncio.wait_for(
                    # Resume the existing message thread rather than rebuilding from a
                    # text summary — no re-reading, no lost tool context.
                    agent.run(envelope, emitter, resume_messages=result.messages, ctx=ctx),
                    timeout=settings.task_timeout_ms / 1000,
                )
            except asyncio.TimeoutError:
                logger.warning("[%s] Continuation %d timed out for subtask %s", run_id, cont, subtask.id)
                return result

            await _add_cost(ctx, next_result)
            result = next_result

            if result.status != AgentStatus.partial:
                return result

        return result