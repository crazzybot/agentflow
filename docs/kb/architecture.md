---
title: Architecture Overview
last_updated: 2026-07-09
last_verified_sha: 88350df
sources:
  - src/agentflow/main.py
  - src/agentflow/orchestrator/
  - src/agentflow/agents/agent.py
  - src/agentflow/core/bus.py
  - src/agentflow/core/context.py
  - src/agentflow/llm/client.py
status: current
---

# Architecture Overview

AgentFlow is a FastAPI service that takes a natural-language task, has an LLM planner
break it into subtasks routed to manifest-defined specialist agents, executes those
subtasks as a dependency-ordered DAG with retries/fallback/budget controls, and
synthesizes the results into a final Markdown report. Everything is in-process
(asyncio), single-process, per-run state — there is no external queue or database.

## Request lifecycle

1. **Entry** — `POST /api/runs` in `src/agentflow/api/routes.py` generates a `run_id`,
   grabs the module-level `engine` singleton (an `OrchestratorEngine`, built once at
   import time in [`main.py`](../../src/agentflow/main.py)), and schedules
   `engine.run(run_id, task, user_context, budget_usd)` as a FastAPI `BackgroundTask`. The
   endpoint returns the `run_id` immediately; clients poll/stream progress via
   `GET /api/runs/{run_id}/stream` (SSE).
2. **Run setup** — `OrchestratorEngine.run()` (in
   [`orchestrator/engine.py`](../../src/agentflow/orchestrator/engine.py)) creates a
   `StreamEmitter` (`stream_registry.create`), a `RunContext` (`context_store.create`),
   and a bus channel (`task_bus.create_run`), then names the run via a cheap Haiku call
   and writes `runs/<run_id>/meta.json`.
3. **Plan** — `create_plan()` in
   [`orchestrator/planner.py`](../../src/agentflow/orchestrator/planner.py) runs an
   agentic ReAct loop (read-only `file_read`/`bash_exec`/`web_search`/`fetch_url` tools)
   against `settings.planner_model` to explore the workspace, then emits a JSON
   `ExecutionPlan` of `Subtask`s (agent id, instruction, `depends_on`, optional
   `budget_fraction`).
4. **Decompose** — `expand_plan()` in
   [`orchestrator/decomposer.py`](../../src/agentflow/orchestrator/decomposer.py)
   expands any subtask whose target `AgentManifest` declares a `decomposition_prompt`
   into several micro-subtasks (again via a scoped `Agent` ReAct loop), and rewires
   `depends_on` edges onto the new tail subtasks.
5. **Schedule + execute** — `OrchestratorEngine._execute_plan()` builds a
   `DependencyGraph` (`orchestrator/scheduler.py`) over the plan and loops: ask the
   graph for `ready()` subtasks (deps satisfied, not failed), dispatch each as an
   `asyncio.Task` via `_dispatch_subtask()`, and reconcile with `asyncio.wait(...,
   FIRST_COMPLETED)` as tasks finish. `_dispatch_subtask()` builds a `TaskEnvelope`
   (instruction + prior results/messages + budget/timeout constraints) and calls
   `Agent.run()`, handling retries with exponential backoff, fallback-agent routing on
   final failure, and budget-exhaustion continuation (`_continue_partial`,
   `_request_budget_increase`) which can pause the run for human input.
6. **Per-subtask agent execution** — `Agent._agentic_loop()` in
   [`agents/agent.py`](../../src/agentflow/agents/agent.py) drives Claude turn-by-turn
   (via the shared `LLMClient`) with the manifest's tools/MCP servers until `end_turn`,
   an iteration limit, or a budget limit is hit, executing any `tool_use` blocks
   concurrently and feeding results back as `tool_result` messages.
7. **Report** — once all subtasks are `completed`/`failed`, the engine gathers
   `ctx.all_results()`, computes a cost summary, and calls `compile_report()` in
   [`orchestrator/reporter.py`](../../src/agentflow/orchestrator/reporter.py), which
   asks `settings.reporter_model` to synthesize leaf-node results (plus any partial/
   failed notes) into `runs/<run_id>/report.md`.
8. **Completion** — the engine emits a `run_complete` (or `run_error`) SSE event via the
   `StreamEmitter`, logs LLM usage stats, and tears down the run's bus/context entries.

## Components

- **`orchestrator/engine.py` — `OrchestratorEngine`**: top-level coordinator; owns the
  shared `LLMClient` and one `Agent` instance per registered manifest; runs the plan →
  schedule → dispatch → report lifecycle and all retry/budget/fallback logic.
- **`orchestrator/planner.py` — `create_plan()`**: turns a task string into an
  `ExecutionPlan` via an LLM ReAct loop with read-only exploration tools; also
  allocates `budget_fraction` per subtask when a run budget is set.
- **`orchestrator/decomposer.py` — `expand_plan()` / `decompose_subtask()`**: optionally
  splits a subtask into micro-subtasks using the agent manifest's own
  `decomposition_prompt`, run as a nested `Agent` loop.
- **`orchestrator/scheduler.py` — `DependencyGraph`**: wraps a `networkx.DiGraph` built
  from `Subtask.depends_on`; validates the plan is acyclic and exposes `ready()`
  (dependency-satisfied, non-failed nodes) for the execution loop.
- **`orchestrator/reporter.py` — `compile_report()`**: synthesizes leaf-subtask results
  (plus partial/failed sections) into the final `report.md` via one more LLM call.
- **`orchestrator/stream.py` — `StreamEmitter` / `StreamRegistry`**: per-run SSE event
  buffer; `emit()` queues an `SSEEvent` for the `/stream` endpoint and optionally
  appends it to `runs/<run_id>/events.jsonl`.
- **`agents/agent.py` — `Agent`**: single generic, manifest-driven class for every agent
  type; runs the tool-calling loop against Claude, tracks token/cost usage per call,
  and returns an `AgentResult` (`success`/`partial`/`failed`).
- **`core/bus.py` — `TaskBus`**: in-process asyncio-queue pair (dispatch/result) keyed
  by `run_id`; documented as swappable for Redis Streams without changing callers. Not
  currently on the request's critical path (dispatch is direct-call via
  `_dispatch_subtask`), but the per-run channels are created/closed alongside the run.
- **`core/context.py` — `RunContext` / `ContextStore`**: per-run shared state — stores
  each subtask's `AgentResult`, running cost totals and budget, and the human-input
  request/response handshake (`request_human_input`/`await_human_input`).
- **`llm/client.py` — `LLMClient`**: wraps `anthropic.AsyncAnthropic.messages.create()`
  with a per-model sliding-window `RateLimiter` and automatic prompt-cache
  `cache_control` injection on system/tool blocks; tracks cumulative `UsageStats`.

## Data flow & messaging

Within one run, components talk through three mechanisms:

- **Shared state — `core/context.py`**: `RunContext` is the source of truth for a run.
  `_dispatch_subtask()` writes each subtask's `AgentResult` via `ctx.store_result()`
  (optionally appended to `runs/<run_id>/results.jsonl`); downstream subtasks read
  dependency output via `ctx.build_prior_results()` (text-only summaries) or
  `ctx.build_prior_messages()` (full conversation replay when there is exactly one
  dependency). `RunContext` also tracks `total_cost_usd()`/`remaining_budget_usd()` and
  arbitrates human-input requests when a budget is exhausted.
- **Events — `core/bus.py` / `orchestrator/stream.py`**: `TaskBus` gives each run an
  asyncio dispatch/result queue pair (`create_run`/`close_run`), intended as the seam
  for a future distributed worker model. Live progress that the HTTP layer actually
  streams to clients flows through `StreamEmitter.emit()` instead — every planning
  step, dispatch, tool call, retry, and completion emits an `SSEEvent` that is both
  queued for `/api/runs/{run_id}/stream` and persisted to `events.jsonl` when
  `settings.capture_events` is set.
- **LLM calls — `llm/client.py`**: every planner, decomposer, agent, and reporter call
  goes through the single shared `LLMClient` instance created in
  `OrchestratorEngine.__init__`, so rate limiting and prompt-cache stats are global
  across the whole run (and process).

## Related

- [codebase-map](codebase-map.md) — directory-by-directory map of the source tree.
- [concepts](concepts.md) — definitions of `Subtask`, `ExecutionPlan`, `AgentManifest`,
  `AgentResult`, and other core model types referenced above.
