---
title: Architecture Overview
last_updated: 2026-07-15
last_verified_sha: 17a27d3
sources:
  - src/agentflow/main.py
  - src/agentflow/orchestrator/
  - src/agentflow/agents/agent.py
  - src/agentflow/core/bus.py
  - src/agentflow/core/context.py
  - src/agentflow/core/models.py
  - src/agentflow/llm/client.py
  - src/agentflow/tools/
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
   and writes `runs/<run_id>/meta.json`. Two `ContextVar` tokens are then armed for the
   lifetime of the run: `_current_sink` (artifact tracking, from
   `tools/artifact_tracker.py`) and `_kb_dispatch_fn` (from `tools/kb_dispatcher.py`),
   which is set to an async closure that dispatches an ad-hoc `KnowledgebaseAgent` subtask
   via `_dispatch_subtask()` — enabling built-in tools (e.g. `download_document`) to
   trigger KB ingest without a direct reference to the engine. `_kb_dispatch_fn` is only
   armed when `KnowledgebaseAgent` is registered in `_agent_instances`.
3. **Plan** — `create_plan()` in
   [`orchestrator/planner.py`](../../src/agentflow/orchestrator/planner.py) builds an
   in-memory `AgentManifest` (tools: `file_read`/`bash_exec_readonly`/`web_search`/`fetch_url`;
   model: `settings.planner_model`; `max_iterations`: `settings.planner_max_iterations`) and
   delegates to a one-shot `Agent.run()` call rather than maintaining its own ReAct loop.
   The planner's system prompt instructs the model to explore the workspace (5-8 tool calls)
   and then emit a JSON `ExecutionPlan` of `Subtask`s (agent id, instruction, `depends_on`,
   optional `budget_fraction`). SSE events (tool calls, thought blocks) flow through the same
   `StreamEmitter` infrastructure as every other agent, with `agent_id="planner"`. If the
   agent's `output.structured` has no `"subtasks"` key, `create_plan()` raises `RuntimeError`
   (no silent fallback); the engine catches it and emits `run:error`.
4. **Decompose** — decomposition is now **lazy**: `engine.run()` no longer calls
   `expand_plan()` eagerly. Instead, `_dispatch_subtask()` calls `decompose_subtask()`
   for any manifest with a `decomposition_prompt` at dispatch time — after the subtask's
   `depends_on` are satisfied — so the decomposer sees the workspace in its completed
   state (see step 5 and the decomposer component entry below). The engine passes
   `task=ctx.task` so the decomposer has the full top-level task framing. `decompose_subtask()`
   now returns `(list[Subtask], context: str)` where `context` is the synthesised
   `<decomposer_context>` block the decomposer wrote before its JSON array. When non-empty,
   the engine prepends this block as `<workspace_context>…</workspace_context>` to every
   micro-task instruction (and to the original subtask instruction in the single-task
   fallback), so agents do not re-read design documents independently.
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
   its `run()` call), and partial-result continuation (`_continue_partial`,
   `_request_budget_increase`). HITL is triggered when the remaining USD budget is at or
   below `agent_min_iteration_budget_usd` **or** when `AgentResult.hit_max_tokens` is
   True (because continuing with the same budget would compute the same `max_tokens` cap
   and hit the wall again). Iteration-limit partials with ample budget remaining continue
   automatically via `_continue_partial` without interrupting the user.
   `ctx.deregister_agent(agent_id)` is always called in a `finally` block when the
   subtask finishes. If the run is cancelled (`asyncio.CancelledError` propagates from
   `asyncio.wait`), `_execute_plan` cancels all in-flight subtask tasks and re-raises;
   `engine.run()` catches it, emits `run:cancelled`, and proceeds to normal cleanup.
6. **Per-subtask agent execution** — `Agent._agentic_loop()` in
   [`agents/agent.py`](../../src/agentflow/agents/agent.py) drives Claude turn-by-turn
   (via the shared `LLMClient`) with the manifest's tools/MCP servers until `end_turn`,
   an iteration limit, or a budget limit is hit, executing any `tool_use` blocks
   concurrently via `_checked_call_tool()` and feeding results back as `tool_result`
   messages. Before each tool dispatch, `_checked_call_tool()` consults the per-loop
   `tool_call_counts` dict against `AgentManifest.tool_limits` and short-circuits with
   an error result if a per-tool call budget is exceeded. If
   `AgentManifest.thinking_budget_tokens` is set, each `messages.create()` call
   includes `thinking={"type": "enabled", "budget_tokens": N}` and — when the manifest
   also has tools — `betas=["interleaved-thinking-2025-05-14"]`; `max_tokens` is
   automatically clamped to at least `thinking_budget_tokens + 1024`.
   Response content is converted to plain dicts via `_to_dict_content()` before
   being stored in the message history — SDK objects are never kept (thinking-block
   `signature` fields are preserved exactly, as the API requires them echoed back
   unchanged). Per non-`end_turn` turn, thinking blocks and text blocks are each
   accumulated into a single `thinking_text` string and emitted as **one** combined
   `agent:thought` SSE event (with `turn_index`); for `end_turn` turns only
   `final_text` is collected from text blocks and no thought event is emitted.
   Multiple text blocks within one response are concatenated (`+=`) rather than
   overwritten. After each tool-result batch the loop
   calls `ctx.pop_user_message(self.agent_id)` and — if a message is queued for this
   agent — appends it as a user turn before the next API call, emitting
   `run:message_received`. Because `push_user_message()` fans out to every registered
   agent's queue, all agents running in parallel receive the same injected message.
   **Event correlation**: every event emitted inside the agentic loop carries a
   `turn_index` equal to the 1-based LLM call iteration (the pre-loop
   `agent:progress` "Starting:" event uses `turn_index=0`); tool call
   `agent:progress` events also carry `tool_call_id` (the Anthropic `tool_use` block
   ID); once the tool returns, a matching `agent:tool_result` event is emitted with
   the same `tool_call_id` and `turn_index`, letting clients pair call and result and
   group all events from one LLM turn together. The budget-exhausted short-circuit
   path also emits an `agent:tool_result` event (with `data.budget_exhausted=true`)
   so the client always sees a paired result.
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
  `ExecutionPlan` by delegating to `Agent.run()` with an in-memory `AgentManifest`
  (read-only tools, `settings.planner_model`, `settings.planner_max_iterations`).
  The planner owns the system prompt and instruction construction (injecting the full
  agent roster as `Available Agents:`, prior-run context as a "Prior Run" prose block,
  and any extra user context as a JSON block); `Agent` handles the ReAct loop, prompt
  caching, SSE emission, and JSON extraction via `_parse_final_output()`. Budget
  fraction normalization (equal split when omitted; renormalization when fractions do not
  sum to 1) runs on the parsed subtask list. If `output.structured` has no `"subtasks"`
  key, `create_plan()` raises `RuntimeError` — the engine emits `run:error`.
- **`orchestrator/decomposer.py` — `decompose_subtask()` / `expand_plan()`**: splits a
  subtask into micro-subtasks using the manifest's `decomposition_prompt`, run as a
  nested `Agent` ReAct loop. Invoked **lazily** inside `_dispatch_subtask()` (not
  eagerly at plan time) so the decomposer always sees completed upstream workspace state.
  `_DECOMPOSER_TOOLS` is `frozenset({"file_read", "bash_exec_readonly"})` — read-only
  exploration only; no writes or arbitrary code execution. `decompose_subtask()` returns
  `tuple[list[Subtask], str]`: the subtask list and a `context` string parsed from a
  `<decomposer_context>…</decomposer_context>` block the decomposer writes before its
  JSON array. `_extract_context_block()` pulls the block; `_strip_context_block()` removes
  it before `_extract_json_array()` parses the micro-task list (preventing brackets inside
  the context prose from confusing the array parser). When decomposition produces N > 1
  micro-subtasks, `_run_micro_subtasks()` schedules them as a **DAG** (using the same
  `DependencyGraph` scheduler as the top-level plan) so parallel branches within a
  decomposed subtask run concurrently. The sink micro-task — the one no other
  micro-task declares as a dependency — is promoted to the parent subtask ID so
  downstream tasks find the aggregated result. A `_skip_decompose=True` flag on
  `_dispatch_subtask` prevents recursive decomposition when micro-subtasks are dispatched.
  Decomposition prompts must produce: (a) a `<decomposer_context>` block with key facts
  from workspace exploration (architecture, layout, interfaces), (b) parallel branches for
  independent work — tasks requiring > 3 output files MUST be split, (c) each micro-task
  writing ≤ 3 files, and (d) a final aggregator micro-task whose `dependsOn` lists every
  other micro-task ID.
- **`orchestrator/scheduler.py` — `DependencyGraph`**: wraps a `networkx.DiGraph` built
  from `Subtask.depends_on`; validates the plan is acyclic and exposes `ready()`
  (dependency-satisfied, non-failed nodes) for the execution loop.
- **`orchestrator/reporter.py` — `compile_report()`**: synthesizes leaf-subtask results
  (plus partial/failed sections) into the final `report.md` via one more LLM call.
- **`orchestrator/stream.py` — `StreamEmitter` / `StreamRegistry`**: per-run SSE event
  buffer; `emit()` appends an `SSEEvent` to an in-memory list and signals an
  `asyncio.Event` so waiting consumers are unblocked without polling (multiple
  consumers replay independently from position 0). `emit()` accepts optional
  `turn_index` (1-based LLM call iteration) and `tool_call_id` (Anthropic
  `tool_use` block ID) which are stored directly on `SSEEvent`; events are also
  appended to `runs/<run_id>/events.jsonl` when `settings.capture_events` is set.
  `stream_registry` is built by a `_make_stream_registry()` factory that returns a
  Redis-Streams-backed `RedisStreamRegistry` (`stream_redis.py`) when
  `STATE_BACKEND=redis`; the registry also exposes an async `connect()` for
  cross-replica streaming.
- **`agents/agent.py` — `Agent`**: single generic, manifest-driven class for every agent
  type; runs the tool-calling loop against Claude, tracks token/cost usage per call,
  and returns an `AgentResult` (`success`/`partial`/`failed`). The model used for each
  API call is `manifest.model or settings.agent_model`, so manifests (e.g. the planner)
  can declare a per-agent model override via `AgentManifest.model`. The loop dispatches
  tool calls through `_checked_call_tool()`, which enforces per-tool call budgets declared
  in `AgentManifest.tool_limits` by incrementing an in-loop counter and returning a hard
  error result (without invoking the tool) when the limit is exceeded. When
  `AgentManifest.thinking_budget_tokens` is set, extended thinking is enabled on every
  LLM call; thinking blocks are emitted as `agent:thought` SSE events and kept in the
  message history for subsequent turns. Thinking tokens are extracted via
  `usage.thinking_tokens` (via `getattr` for forward-compatibility). `AgentResult`
  carries `thinking_tokens` as a separate counter (a subset of `output_tokens`) and a
  `hit_max_tokens: bool` flag (set when `stop_reason=="max_tokens"`); thinking tokens are
  priced at `cost_per_1m_thinking_tokens` while regular output tokens use
  `cost_per_1m_output_tokens`. On `max_tokens`, the partial assistant message is popped
  from history (resumption starts from the last clean state), but pending tool calls ARE
  still dispatched so clients receive paired `agent:progress` / `agent:tool_result` SSE
  events — the results are not appended to history. SSE events emitted during the loop
  carry `turn_index` (1-based LLM call counter); `agent:progress` tool-call events carry
  `tool_call_id` (the Anthropic `tool_use` block ID); `_call_tool()` emits a matching
  `agent:tool_result` event (same `tool_call_id`) after the tool returns so clients can
  pair call and result; the budget-exhausted path also emits `agent:tool_result` with
  `data.budget_exhausted=true`. Three additional helpers manage output tracking and
  parsing: `_to_dict_content()` converts SDK response blocks to plain dicts on
  storage (preserving thinking-block `signature` fields) so the history can be
  inspected without SDK coupling; `_collect_written_paths()` identifies file paths
  from successful `file_write` tool calls this turn (cross-referencing blocks
  against tool results that lack an `"error"` prefix) and appends them to the
  per-run `all_files_written` accumulator, which is returned as
  `AgentResult.files_written` — the full written content is kept verbatim in the
  message history (no truncation or compaction); `_format_upstream_context()` builds
  the `<upstream_context>` XML block injected into a downstream agent's initial user
  message, combining text summaries from `prior_results` with file paths from
  `upstream_artifacts` so the agent knows both what happened and which files to read;
  `_parse_final_output()` splits the model's final text into
  `(structured: dict, prose: str)` — it handles raw JSON, markdown-fenced JSON (the
  common case where the model prepends a summary), and inline JSON without a fence,
  so `AgentResult.output.structured` is reliably populated and `output.text` contains
  only the human-readable prose.
- **`tools/builtin.py` + `tools/arxiv_search.py` — built-in tool layer**: registers all
  built-in `ToolDefinition`s into the global `tool_registry`. Key tools:
  - `arxiv_search` — searches arXiv Atom API; returns `title`, `abstract`, `url` (abs),
    and `pdf_url` (derived by replacing `/abs/` with `/pdf/` in the abs URL, normalised to
    https). Use `category=` to restrict to a subject area and avoid off-topic hits. The
    abstract is returned directly — agents should NOT call `fetch_url` on arxiv links.
  - `download_document` — fetches a PDF/text/markdown URL and saves it to `.downloads/`
    in the workspace; triggers KB ingest automatically via the `_kb_dispatch_fn` ContextVar
    if `KnowledgebaseAgent` is active in the run. Returns the workspace-relative path and
    the KB ingest outcome.
  - `fetch_url` — transparently redirects `arxiv.org/abs/` URLs to the Atom API so
    agents that accidentally call it on an arXiv link still get structured text.
  - `file_write` — multi-mode writer (`overwrite`, `append`, `replace_lines`, etc.).
    Artifacts recorded via `_record_artifact()` which writes to the `_current_sink`
    ContextVar set by the engine per run.
  - `_kb_dispatch_fn` (`tools/kb_dispatcher.py`) — a `ContextVar` holding the per-run
    KB dispatch callable; set to `None` when `KnowledgebaseAgent` is not configured.
- **`core/bus.py` — `TaskBus`**: in-process asyncio-queue pair (dispatch/result) keyed
  by `run_id`. Not currently on the request's critical path (dispatch is direct-call via
  `_dispatch_subtask`), but the per-run channels are created/closed alongside the run. A
  `_make_task_bus()` factory returns the Redis-backed `RedisTaskBus` (`bus_redis.py`)
  when `STATE_BACKEND=redis`; both remain the future seam for a worker-pool split.
- **`core/context.py` — `RunContext` / `ContextStore`**: per-run shared state — stores
  each subtask's `AgentResult`, running cost totals and budget, the human-input
  request/response handshake (`request_human_input`/`await_human_input`/async
  `provide_human_input`), and the top-level `task: str` (stored as `ctx.task`; set at
  creation time and forwarded to the decomposer so it has full task framing without
  needing to re-explore the workspace). `context_store` is built by `_make_context_store()`,
  which returns a write-through `RedisContextStore` (`context_redis.py`) under
  `STATE_BACKEND=redis`; `ContextStore.connect()` enables cross-replica HITL delivery.
- **`llm/client.py` — `LLMClient`**: wraps the Anthropic SDK with automatic prompt-cache
  `cache_control` injection on system/tool blocks and tracks cumulative `UsageStats`
  (fields: `total_requests`, `total_input_tokens`, `total_output_tokens`,
  `total_thinking_tokens`, `cache_creation_tokens`, `cache_read_tokens`). Internally
  uses `messages.stream()` / `beta.messages.stream()` + `get_final_message()` instead
  of `messages.create()` so that long agent turns are not cut off by the Anthropic SDK's
  10-minute non-streaming timeout. Rate limiting is delegated to the Anthropic SDK
  (`max_retries=4`, exponential backoff on 429/500) rather than a per-process limiter,
  which would not coordinate across replicas.

## Data flow & messaging

Within one run, components talk through three mechanisms:

- **Shared state — `core/context.py`**: `RunContext` is the source of truth for a run.
  `_dispatch_subtask()` writes each subtask's `AgentResult` via `ctx.store_result()`
  (optionally appended to `runs/<run_id>/results.jsonl`); downstream subtasks read
  dependency output via `ctx.build_prior_results()` (combined prose + structured-JSON
  summaries keyed by dep task ID — both `output.text` and `output.structured` are
  included when present so the synthesizer sees the full agent output) and
  `ctx.build_upstream_artifacts()` (dict of dep task ID → `files_written` list from
  that task's `AgentResult`). The agent receives both as an `<upstream_context>` block
  in its initial user message — it reads the exact files it needs rather than receiving
  the full upstream conversation history. `RunContext`
  also tracks `total_cost_usd()`/`remaining_budget_usd()` and arbitrates human-input
  requests when a budget is exhausted. Mid-run user messages (from `POST …/message`)
  are stored in per-agent queues: `register_agent(agent_id)` creates a queue when a
  subtask starts; `push_user_message(content)` fans the message out to every
  registered agent's queue; `pop_user_message(agent_id)` drains one message from that
  agent's own queue; `deregister_agent(agent_id)` removes the queue when the subtask
  finishes. This guarantees all parallel agents receive the same injected message.
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
