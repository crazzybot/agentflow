---
title: Architecture Overview
last_updated: 2026-07-10
last_verified_sha: 45e5fe8
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
synthesizes the results into a final Markdown report. By default all per-run state
is in-process (asyncio), single-process, with no external queue or database. Setting
`STATE_BACKEND=redis` swaps the bus, context store, and SSE registry for Redis-backed
variants so multiple API replicas can share a run — see
[subsystems/redis-backend](subsystems/redis-backend.md).

## Request lifecycle

1. **Entry** — `POST /api/runs` in `src/agentflow/api/routes.py` generates a `run_id`,
   grabs the module-level `engine` singleton (an `OrchestratorEngine`, built once at
   import time in [`main.py`](../../src/agentflow/main.py)), and schedules
   `engine.run(run_id, task, user_context, budget_usd)` with `asyncio.create_task` (so it
   starts during the brief emitter-creation poll, unlike a FastAPI `BackgroundTask` which
   would only run after the response is sent). The endpoint returns the `run_id`
   immediately; clients poll/stream progress via `GET /api/runs/{run_id}/stream` (SSE).
2. **Run setup** — `OrchestratorEngine.run()` (in
   [`orchestrator/engine.py`](../../src/agentflow/orchestrator/engine.py)) registers the
   current asyncio Task in `self._run_tasks[run_id]` (used by `cancel_run()`), creates a
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
   FIRST_COMPLETED)` as tasks finish. `_dispatch_subtask()` calls
   `ctx.register_agent(agent_id)` so mid-run user messages are routed to that agent,
   builds a `TaskEnvelope` (instruction + prior results/messages + budget/timeout
   constraints; planner-only context keys such as `prior_report` and
   `prior_subtask_outputs` are stripped before the envelope is built so agents only
   receive user-supplied extra context), and calls `Agent.run(envelope, emitter,
   ctx=ctx)`. It handles retries with exponential backoff, fallback-agent routing on
   final failure (deregistering the primary agent and registering the fallback before
   its `run()` call), and budget-exhaustion continuation (`_continue_partial`,
   `_request_budget_increase`) which can pause the run for human input.
   `ctx.deregister_agent(agent_id)` is always called in a `finally` block when the
   subtask finishes. If the run is cancelled (`asyncio.CancelledError` propagates from
   `asyncio.wait`), `_execute_plan` cancels all in-flight subtask tasks and re-raises;
   `engine.run()` catches it, emits `run:cancelled`, and proceeds to normal cleanup.
6. **Per-subtask agent execution** — `Agent._agentic_loop()` in
   [`agents/agent.py`](../../src/agentflow/agents/agent.py) drives Claude turn-by-turn
   (via the shared `LLMClient`) with the manifest's tools/MCP servers until `end_turn`,
   an iteration limit, or a budget limit is hit, executing any `tool_use` blocks
   concurrently and feeding results back as `tool_result` messages. After each tool-result
   batch the loop calls `ctx.pop_user_message(self.agent_id)` and — if a message is
   queued for this agent — appends it as a user turn before the next API call, emitting
   `run:message_received`. Because `push_user_message()` fans out to every registered
   agent's queue, all agents running in parallel receive the same injected message.
7. **Report** — once all subtasks are `completed`/`failed`, the engine gathers
   `ctx.all_results()`, computes a cost summary, and calls `compile_report()` in
   [`orchestrator/reporter.py`](../../src/agentflow/orchestrator/reporter.py), which
   asks `settings.reporter_model` to synthesize leaf-node results (plus any partial/
   failed notes) into `runs/<run_id>/report.md`.
8. **Completion** — the engine emits `run_complete` (or `run_error`, or `run_cancelled`
   if the run was cancelled mid-flight) via the `StreamEmitter`, logs LLM usage stats,
   removes the task from `_run_tasks`, and tears down the run's bus/context entries.

## Components

- **`orchestrator/engine.py` — `OrchestratorEngine`**: top-level coordinator; owns the
  shared `LLMClient`, one `Agent` instance per registered manifest, and `_run_tasks`
  (a `dict[run_id, asyncio.Task]` used by `cancel_run()` to cancel active runs); runs
  the plan → schedule → dispatch → report lifecycle and all retry/budget/fallback/cancel
  logic.
- **`orchestrator/planner.py` — `create_plan()`**: turns a task string into an
  `ExecutionPlan` via an LLM ReAct loop with read-only exploration tools; also
  allocates `budget_fraction` per subtask when a run budget is set. For follow-up
  runs, prior-run context keys (`prior_report`, `prior_task`, `prior_run_id`,
  `prior_subtask_outputs`) are extracted from `user_context` and formatted as a
  readable "Prior Run" prose section in the planner's first message; any remaining
  user-supplied context keys appear as a separate JSON block.
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
  appends it to `runs/<run_id>/events.jsonl`. `stream_registry` is built by a
  `_make_stream_registry()` factory that returns a Redis-Streams-backed
  `RedisStreamRegistry` (`stream_redis.py`) when `STATE_BACKEND=redis`; the registry
  also exposes an async `connect()` for cross-replica streaming.
- **`agents/agent.py` — `Agent`**: single generic, manifest-driven class for every agent
  type; runs the tool-calling loop against Claude, tracks token/cost usage per call,
  and returns an `AgentResult` (`success`/`partial`/`failed`).
- **`core/bus.py` — `TaskBus`**: in-process asyncio-queue pair (dispatch/result) keyed
  by `run_id`. Not currently on the request's critical path (dispatch is direct-call via
  `_dispatch_subtask`), but the per-run channels are created/closed alongside the run. A
  `_make_task_bus()` factory returns the Redis-backed `RedisTaskBus` (`bus_redis.py`)
  when `STATE_BACKEND=redis`; both remain the future seam for a worker-pool split.
- **`core/context.py` — `RunContext` / `ContextStore`**: per-run shared state — stores
  each subtask's `AgentResult`, running cost totals and budget, and the human-input
  request/response handshake (`request_human_input`/`await_human_input`/async
  `provide_human_input`). `context_store` is built by `_make_context_store()`, which
  returns a write-through `RedisContextStore` (`context_redis.py`) under
  `STATE_BACKEND=redis`; `ContextStore.connect()` enables cross-replica HITL delivery.
- **`llm/client.py` — `LLMClient`**: wraps `anthropic.AsyncAnthropic.messages.create()`
  with automatic prompt-cache `cache_control` injection on system/tool blocks and tracks
  cumulative `UsageStats`. Rate limiting is delegated to the Anthropic SDK
  (`max_retries=4`, exponential backoff on 429/500) rather than a per-process limiter,
  which would not coordinate across replicas.

## Data flow & messaging

Within one run, components talk through three mechanisms:

- **Shared state — `core/context.py`**: `RunContext` is the source of truth for a run.
  `_dispatch_subtask()` writes each subtask's `AgentResult` via `ctx.store_result()`
  (optionally appended to `runs/<run_id>/results.jsonl`); downstream subtasks read
  dependency output via `ctx.build_prior_results()` (text-only summaries) or
  `ctx.build_prior_messages()` (full conversation replay when there is exactly one
  dependency). `RunContext` also tracks `total_cost_usd()`/`remaining_budget_usd()` and
  arbitrates human-input requests when a budget is exhausted. Mid-run user messages
  (from `POST …/message`) are stored in per-agent queues: `register_agent(agent_id)`
  creates a queue when a subtask starts; `push_user_message(content)` fans the message
  out to every registered agent's queue; `pop_user_message(agent_id)` drains one message
  from that agent's own queue; `deregister_agent(agent_id)` removes the queue when the
  subtask finishes. This guarantees all parallel agents receive the same injected message.
- **Events — `core/bus.py` / `orchestrator/stream.py`**: `TaskBus` gives each run an
  asyncio dispatch/result queue pair (`create_run`/`close_run`), intended as the seam
  for a future distributed worker model. Live progress that the HTTP layer actually
  streams to clients flows through `StreamEmitter.emit()` instead — every planning
  step, dispatch, tool call, retry, and completion emits an `SSEEvent` that is both
  queued for `/api/runs/{run_id}/stream` and persisted to `events.jsonl` when
  `settings.capture_events` is set.
- **LLM calls — `llm/client.py`**: every planner, decomposer, agent, and reporter call
  goes through the single shared `LLMClient` instance created in
  `OrchestratorEngine.__init__`, so prompt-cache breakpoints and `UsageStats` are global
  across the whole run (and process); throttling/retries are handled inside the SDK.

## Related

- [conventions](conventions.md) — code style, async patterns, testing rules.
- [subsystems/redis-backend](subsystems/redis-backend.md) — the optional Redis state
  backend (bus, context store, SSE registry) and cross-replica HITL/streaming.
- [`/how-to` skill](../../.claude/skills/how-to/SKILL.md) — recipes for adding agents, tools, and skills.
