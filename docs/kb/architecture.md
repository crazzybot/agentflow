---
title: Architecture Overview
last_updated: 2026-09-30
last_verified_sha: c5e3b74
sources:
  - src/agentflow/main.py
  - src/agentflow/api/routes.py
  - src/agentflow/orchestrator/
  - src/agentflow/agents/agent.py
  - src/agentflow/core/bus.py
  - src/agentflow/core/context.py
  - src/agentflow/core/prior_results.py
  - src/agentflow/core/models.py
  - src/agentflow/core/registry.py
  - src/agentflow/core/skill_loader.py
  - src/agentflow/cli/
  - src/agentflow/llm/client.py
  - src/agentflow/tools/
  - manifests/
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
   and a bus channel (`task_bus.create_run`), then kicks off `_generate_run_name()` and
   `_is_single_agent_task()` concurrently via `asyncio.create_task` — both are independent
   cheap `settings.reporter_model` calls that only need the task string, so running them
   together (and alongside the setup below) avoids stacking their latency sequentially
   before real work starts. `_generate_run_name()`'s result is awaited immediately to
   write `<runs_dir>/<run_id>/meta.json`; `_is_single_agent_task()`'s result is awaited later,
   right before it gates plan-vs-direct routing (step 3). Two `ContextVar` tokens are then armed for the
   lifetime of the run: `_current_sink` (artifact tracking, from
   `tools/artifact_tracker.py`) and `_kb_dispatch_fn` (from `tools/kb_dispatcher.py`),
   which is set to an async closure that dispatches an ad-hoc `KnowledgebaseAgent` subtask
   via `_dispatch_subtask()` — enabling built-in tools (e.g. `download_document`) to
   trigger KB ingest without a direct reference to the engine. `_kb_dispatch_fn` is only
   armed when `KnowledgebaseAgent` is registered in `_agent_instances`.
3. **Plan** — when `settings.direct_agent_id` (`DIRECT_AGENT_ID`) is set, the run is
   auto-classified first: the `is_direct_task` result awaited from step 2
   (`_is_single_agent_task()`) decides whether the planner runs at all. When it is
   unset (the default), the classifier task is never created — no Haiku call — and
   every run goes straight to the planner.
   - If `True` (auto-classified as a single-agent task), `_make_direct_plan()` builds a
     synthetic one-subtask `ExecutionPlan` that routes the whole task verbatim to
     `settings.direct_agent_id` (`DIRECT_AGENT_ID` in `.env`) — `create_plan()` is skipped
     entirely, so there's zero planner LLM overhead. `_make_direct_plan()` raises
     `RuntimeError` if `direct_agent_id` does not name a registered agent (the engine
     catches it and emits `run:error`) — a misconfigured `DIRECT_AGENT_ID` fails loudly
     rather than silently falling back to the planner.
   - Otherwise, `create_plan()` in
     [`orchestrator/planner.py`](../../src/agentflow/orchestrator/planner.py) builds an
     in-memory `AgentManifest` (tools: `file_read`/`bash_exec_readonly`/`web_search`/`fetch_url`;
     model: `settings.planner_model`; `max_iterations`: `settings.planner_max_iterations`) and
     delegates to a one-shot `Agent.run()` call rather than maintaining its own ReAct loop.
     The planner's system prompt instructs the model to explore the workspace (5-8 tool calls)
     and then emit a JSON `ExecutionPlan` of `Subtask`s (agent id, instruction, `depends_on`,
     optional `budget_fraction`, and optional `model_tier`/`thinking_effort` cost-tuning
     overrides — see the cost-tiering paragraph under the `Agent` component below). SSE events
     (tool calls, thought blocks) flow through the same `StreamEmitter` infrastructure as every
     other agent, with `agent_id="planner"`. If the agent's `output.structured` has no
     `"subtasks"` key, `create_plan()` raises `RuntimeError` (no silent fallback); the engine
     catches it and emits `run:error`.

   Either branch emits `plan:created` (message text distinguishes "Single-agent mode:
   planner skipped (auto-classified)" from the subtask count).
4. **Decompose** — gated by `settings.enable_decomposer` (`ENABLE_DECOMPOSER` in `.env`,
   default `true`); setting it `false` skips this step entirely and every subtask runs
   exactly as the planner (or direct-mode) produced it, with no extra ReAct loop. When
   enabled, decomposition is **lazy**: `engine.run()` does not call `expand_plan()`
   eagerly. Instead, `_dispatch_subtask()` calls `decompose_subtask()`
   for any manifest with a `decomposition_prompt` at dispatch time — after the subtask's
   `depends_on` are satisfied — so the decomposer sees the workspace in its completed
   state (see step 5 and the decomposer component entry below). The engine passes
   `task=ctx.task` so the decomposer has the full top-level task framing. `decompose_subtask()`
   returns `(list[Subtask], context: str)` where `context` is the synthesised
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
   constraints; the planner-only follow-up keys `prior_run_id`, `prior_task`,
   `prior_report` and `prior_subtask_outputs` are stripped before the envelope is built so agents only
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
   an error result if a per-tool call budget is exceeded. If the resolved
   `thinking_effort` (see the `Agent` component's cost-tiering precedence below —
   `envelope.constraints.thinking_effort` if the planner set one for this subtask, else
   `AgentManifest.thinking_effort`, else `settings.agent_thinking_effort`, which defaults
   to `"medium"`) is non-empty **and** `supports_adaptive_thinking(resolved_model)` (in
   `llm/client.py` — an allowlist of model-id prefixes; Haiku 4.5 and older reject
   adaptive thinking and `effort` with a 400, so e.g. a `thinking_effort: high` agent
   re-tiered to `"economy"` sends neither), each `messages.create()` call includes adaptive thinking —
   `thinking={"type": "adaptive", "display": "summarized"}` plus
   `output_config={"effort": thinking_effort}` — with no beta header (adaptive thinking
   auto-enables interleaved thinking on current-gen models). `display="summarized"` is
   required: the API's default (`"omitted"`) would stream `thinking` blocks with empty
   text, silently breaking both the `agent:thought` events and thinking-token accounting
   below. Unlike the old `budget_tokens` style, adaptive thinking has no numeric budget to
   clamp `max_tokens` to or shrink under a tight per-task budget — instead, for any
   iteration whose budget-derived `max_tokens` is below 1024 (`_budget_to_max_tokens()`
   can floor it as low as 256 near budget exhaustion) the `thinking` config is dropped and
   only `output_config={"effort": "low"}` is sent. Merely omitting the parameters would not
   turn thinking off: Sonnet 5 / Opus 5+ think adaptively at their default `"high"` effort
   when `thinking` is absent. The global default is explicit (`"medium"`) for the same
   reason — `""` means "omit and use the model's own default", which differs by model.
   If a run ends with no text block, the final output falls back to the last non-empty
   `thinking` block.
   Response content is converted to plain dicts via `_to_dict_content()` before
   being stored in the message history — SDK objects are never kept (thinking-block
   `signature` fields are preserved exactly, as the API requires them echoed back
   unchanged). Every turn (including `end_turn`), only `thinking`-typed blocks are
   accumulated into a `thinking_text` string and emitted as an `agent:thought` SSE
   event (with `turn_index`) before any stop-reason branch — a turn that ends via
   `end_turn` without calling a tool still surfaces its reasoning. `final_text` is
   collected separately from `text` blocks and is never mixed into the thought event.
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
   synthesizes leaf-node results (plus any partial/failed notes) into
   `<runs_dir>/<run_id>/report.md`. The synthesis model is `settings.report_model` if set, else
   `settings.report_model_tier` (default `"standard"`) resolved via `resolve_model_tier()`
   — not the cheap `reporter_model`, which is only used for run naming and direct/plan
   routing — with `max_tokens=settings.report_max_tokens` (16k) and
   `output_config.effort=settings.report_thinking_effort` (skipped for models without
   adaptive thinking). A `max_tokens` stop is logged and a visible truncation note is
   appended to the report. Per-result input is capped at `_MAX_RESULT_CHARS` (100k — a
   loudly-logged safety ceiling, not a routine trim).
8. **Completion** — the engine emits `run:complete` (or `run:error`, or `run:cancelled`
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
  key, `create_plan()` raises `RuntimeError` — the engine emits `run:error`. It also
  raises (before any subtask runs) when a subtask names an agent not in the registry, or
  when `DependencyGraph` rejects the plan (duplicate ids, a `dependsOn` naming an id not
  in the plan, or a cycle).
  Each subtask may also carry optional `modelTier`/`thinkingEffort` cost-tuning fields
  (`_COST_TUNING_INSTRUCTIONS` in the system prompt asks the planner to set these only
  when a subtask's difficulty clearly diverges from its target agent's typical work, e.g.
  downgrading a mechanical formatting pass to `"economy"`). `_validate_choice()` checks
  each value against `_VALID_MODEL_TIERS`/`_VALID_THINKING_EFFORTS` and drops (logs a
  warning, does not raise) anything the model gets wrong — these are optimization hints,
  not plan-correctness-critical fields, so a malformed value falls back to the target
  agent's own manifest default rather than failing the whole plan. Parsed onto
  `Subtask.model_tier`/`Subtask.thinking_effort`, then forwarded into
  `TaskConstraints.model_tier`/`.thinking_effort` by `_dispatch_subtask()` — see the
  `Agent` component's cost-tiering paragraph below for how they're resolved.
- **`orchestrator/decomposer.py` — `decompose_subtask()` / `expand_plan()`**: splits a
  subtask into micro-subtasks using the manifest's `decomposition_prompt`, run as a
  nested `Agent` ReAct loop. Invoked **lazily** inside `_dispatch_subtask()` (not
  eagerly at plan time) so the decomposer always sees completed upstream workspace state.
  `_DECOMPOSER_TOOLS` is `frozenset({"file_read", "bash_exec_readonly"})` — read-only
  exploration only; no writes or arbitrary code execution. Its manifest hardcodes
  `model_tier="economy"` — pure workspace exploration plus JSON splitting doesn't need the
  target agent's own (often pricier) tier, and unlike the planner's cost-tuning fields this
  decision is safe as an unconditional default rather than a per-call judgment call. The
  decomposer manifest always
  uses `on_iteration_limit=IterationLimitAction.finalize` (set in `decompose_subtask()`),
  so when it exhausts its exploration iterations it receives a finalization prompt and
  makes one final tool-free LLM call to produce its output instead of silently returning
  nothing. `decomposer_max_iterations` defaults to 10 (raised from 5 — complex workspaces
  can exhaust 5 turns on exploration alone, leaving no turn for the JSON output).
  `decompose_subtask()` returns `tuple[list[Subtask], str]`: the subtask list and a
  `context` string parsed from a `<decomposer_context>…</decomposer_context>` block the
  decomposer writes before its JSON array. `_extract_context_block()` pulls the block;
  `_strip_context_block()` removes it before `_extract_json_array()` parses the
  micro-task list (preventing brackets inside the context prose from confusing the array
  parser). Failure modes are guarded explicitly: `AgentStatus.failed` logs a warning and
  returns the original subtask; `AgentStatus.partial` with empty output logs a specific
  "hit iteration limit" warning and also returns the original subtask (preventing a
  silent `JSONDecodeError` from masking the real cause). Micro-task ids are namespaced
  under the parent (`"{parent.id}.{id}"`, left alone if already prefixed) because results
  are stored per run by subtask id — two parallel decomposed subtasks that both emit
  `"step-1"` would otherwise overwrite each other. Only edges inside the micro-plan are
  kept: the parent's own `depends_on` is never copied in (decomposition runs lazily, so
  those deps are already satisfied), a micro-task without `dependsOn` chains after the
  previous one, and any out-of-plan `dependsOn` entry is dropped with a warning. The
  result is validated with `DependencyGraph`; a cycle or duplicate id falls back to the
  original subtask. (`expand_plan()` — the unused eager path — re-attaches the parent's
  deps to micro-plan roots.) When decomposition produces N > 1
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
  from `Subtask.depends_on`; validates the plan at construction (raises `ValueError` on
  duplicate ids, a `depends_on` id not in the plan — which would otherwise become a
  phantom node that `ready()` returns and then fails to look up — or a cycle) and
  exposes `ready()`
  (dependency-satisfied, non-failed nodes) for the execution loop.
- **`orchestrator/reporter.py` — `compile_report()`**: synthesizes leaf-subtask results
  (plus partial/failed sections) into the final `report.md` via one more LLM call on the
  `report_model` / `report_model_tier` model (see step 7).
- **`orchestrator/stream.py` — `StreamEmitter` / `StreamRegistry`**: per-run SSE event
  buffer; `emit()` appends an `SSEEvent` to an in-memory list and signals an
  `asyncio.Event` so waiting consumers are unblocked without polling (multiple
  consumers replay independently from position 0). `emit()` accepts optional
  `turn_index` (1-based LLM call iteration) and `tool_call_id` (Anthropic
  `tool_use` block ID) which are stored directly on `SSEEvent`; events are also
  appended to `<runs_dir>/<run_id>/events.jsonl` when `settings.capture_events` is set.
  `stream_registry` is built by a `_make_stream_registry()` factory that returns a
  Redis-Streams-backed `RedisStreamRegistry` (`stream_redis.py`) when
  `STATE_BACKEND=redis`; the registry also exposes an async `connect()` for
  cross-replica streaming.
- **`agents/agent.py` — `Agent`**: single generic, manifest-driven class for every agent
  type; runs the tool-calling loop against Claude, tracks token/cost usage per call,
  and returns an `AgentResult` (`success`/`partial`/`failed`).
  **Model/thinking-effort resolution (cost tiering)** — most to least specific:
  1. `manifest.model` — an explicit raw model id is a hard ops-level pin; wins over
     everything, including a planner-assigned override, since it represents a fixed
     requirement (e.g. "this agent must always run on Opus").
  2. `envelope.constraints.model_tier` / `.thinking_effort` — a planner-assigned
     per-subtask-instance override (see `orchestrator/planner.py`'s cost-tuning fields
     above), resolved to a model id via `settings.resolve_model_tier()`.
  3. `manifest.model_tier` / `manifest.thinking_effort` — the agent's own declared
     default tier/effort (e.g. `decompose_subtask()`'s hardcoded `"economy"`).
  4. `settings.agent_model` / `settings.agent_thinking_effort` — the global fallback.
  Named tiers (`"economy"` | `"standard"` | `"premium"`) are defined in `config.py` as
  `model_tier_economy`/`_standard`/`_premium` and resolved by `settings.resolve_model_tier()`,
  which returns `None` for an unset or unrecognised tier name (non-fatal — the caller
  falls through to the next precedence level) rather than raising. Re-tiering an entire
  fleet of manifests is then a three-setting `.env` change rather than editing every
  manifest's raw `model` field. The resolved value is stored once per loop as
  `resolved_model`. Cost accounting uses `_pricing_for(resolved_model)`
  — a model-id-prefix pricing table (Fable/Mythos 5.x, Opus 5.5/5/4.x, Sonnet 5/4.x,
  Haiku 4.5/3; first match wins, so specific prefixes like `claude-opus-5-5` precede
  `claude-opus-5`) that falls back to the flat `settings.cost_per_1m_*` rates (with a
  one-time warning) for unrecognised models — rather than always pricing at
  `agent_model`'s rate, so a manifest override to a different pricing tier is still
  budgeted correctly. Each entry is `(input, output, cache_read_multiplier)`: cache writes
  are 1.25x input (5-minute TTL) and cache reads are input × the per-model multiplier
  (0.1x standard; 0.05x Opus 5.5; 0.025x Fable/Mythos 5.1). Sonnet 5 is $2/$10 — its
  launch "introductory" rate became the permanent price. `_budget_to_max_tokens()` takes
  this same resolved input/output pricing as parameters instead of reading
  `settings.cost_per_1m_*` directly. The loop dispatches
  tool calls through `_checked_call_tool()`, which enforces per-tool call budgets declared
  in `AgentManifest.tool_limits` by incrementing an in-loop counter and returning a hard
  error result (without invoking the tool) when the limit is exceeded. When the resolved
  `thinking_effort` is non-empty (manifest default or planner-assigned override — see the
  cost-tiering precedence above), adaptive extended thinking is enabled on every LLM call
  at that effort level; thinking blocks are emitted as `agent:thought` SSE events
  and kept in the message history for subsequent turns. **Iteration-limit behaviour** is controlled by
  `AgentManifest.on_iteration_limit` (`IterationLimitAction` enum in `core/models.py`):
  - `"stop"` (default) — return `AgentStatus.partial` immediately, as before.
  - `"finalize"` — inject `manifest.iteration_limit_message` (or `_DEFAULT_FINALIZE_MESSAGE`)
    as a user turn, then make **one extra LLM call** with `tool_choice={"type": "none"}`
    so the model is forced to produce text output from whatever it gathered. Returns
    `AgentStatus.partial` with non-empty `output.text`. Used by the decomposer.
  - `"ask_user"` — if `ctx` is available, acquires `ctx.human_input_lock`, emits
    `run:awaiting_input`, and blocks until the user responds via HITL. `action="continue"`
    with `iteration_increase=N` extends `max_iterations` by N and resumes; `action="cancel"`
    returns `AgentStatus.partial` immediately. Falls back to `"stop"` when `ctx` is None.

  Thinking tokens are not broken out in the Messages API `usage` object (they're billed
  as part of `output_tokens`), so both `Agent` and `LLMClient` estimate them via the
  shared `llm.estimate_thinking_tokens()` helper (~4 chars/token over `thinking` block
  text) rather than maintaining two independent copies of the same estimate. `AgentResult`
  carries `thinking_tokens` as a separate counter (a subset of `output_tokens`) and a
  `hit_max_tokens: bool` flag (set when `stop_reason=="max_tokens"`); thinking tokens are
  priced at the resolved model's output rate (see `_pricing_for()` above) — the same rate
  regular output tokens use, since Anthropic bills thinking as output. On `max_tokens`, the partial assistant message is popped
  from history (resumption starts from the last clean state), and pending tool calls are
  **not** executed — their inputs may be truncated (a half-written `bash_exec` command or
  `file_write` payload). Instead one `agent:tool_result` event per call is emitted with
  `data.skipped=true` and the matching `tool_call_id`, so clients still see each call
  resolved. SSE events emitted during the loop
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
  `AgentResult.files_written`. The assistant's own tool_use blocks (including
  `file_write`'s `content` argument) are stored in history verbatim and never
  retroactively edited — mutating a past tool_use.input makes the model read its
  own prior request as truncated, and it reacts identically to a real
  mid-generation cutoff by redundantly retrying the call. `tool_result` content
  (new information *returned by* a tool, not generated by the model) is the only
  side that's ever capped — see `_call_tool()`'s result-size budget below. There
  is still no compaction/summarization of the overall message history as it
  grows across iterations. `_format_upstream_context()` builds
  the `<upstream_context>` XML block injected into a downstream agent's initial user
  message, combining text summaries from `prior_results` with file paths from
  `upstream_artifacts` so the agent knows both what happened and which files to read.
  Because this injection fires once per DAG edge (inlined text multiplies with graph
  depth, unlike a single agent's own tool-result cap), each summary is capped
  separately: at or under `_UPSTREAM_SUMMARY_INLINE_THRESHOLD` (2,000 chars) it's
  inlined in full; above it, it's spilled to disk via the same `write_overflow_file()`
  used for oversized tool results (`.tool_output/upstream_result_<dep_id>.txt`) and
  replaced with a head+tail preview + pointer. This replaced a prior silent
  `str(summary)[:500]` slice that clipped every summary at a fixed size with no
  indication anything was cut and no way to recover the rest — see
  `docs/context-optimization-plan.md`, Fix 2. `upstream_artifacts` paths were never
  inlined (they're already just file paths) and are unaffected.
  `_parse_final_output()` splits the model's final text into
  `(structured: dict, prose: str)` — it handles raw JSON, markdown-fenced JSON (the
  common case where the model prepends a summary), and inline JSON without a fence,
  so `AgentResult.output.structured` is reliably populated and `output.text` contains
  only the human-readable prose.
- **`tools/registry.py` — result-size budget**: `ToolDefinition.max_result_chars`
  (default `8_000`) is the per-tool context budget for `tool_result` content only —
  never applied to tool_use.input (see above). In `Agent._call_tool()`, once a
  handler's return value exceeds its tool's `max_result_chars`,
  `tools/builtin.py`'s `write_overflow_file()` spills the full text to
  `.tool_output/<tool>_<tool_use_id>.txt` in the workspace and returns a head+tail
  preview (not head-only — an error or final result in e.g. bash stdout often lands
  at the end) plus a pointer telling the model to `file_read` the rest. The scratch
  file is deliberately never passed to `_record_artifact()` — it's incidental tool
  output, not a run deliverable, so it stays out of `files_written` /
  `build_upstream_artifacts()` and doesn't bleed into downstream agents' context or
  the final report. `file_read` sets `max_result_chars=None` (exempt) because it
  already manages its own budget — see below.
- **`tools/builtin.py` + `tools/arxiv_search.py` — built-in tool layer**: registers all
  built-in `ToolDefinition`s into the global `tool_registry`. Sandbox boundaries:
  - `_safe_path()` (used by `file_read`/`file_write`) resolves inside the workspace and
    checks containment with `Path.is_relative_to` — not a string prefix, which a sibling
    `workspace-other/` directory would pass. The artifact-content route uses the same check.
  - `bash_exec`/`python_exec` run with `_sandbox_env()`: an allowlist (`PATH`, `LANG`,
    `LC_*`, `TERM`, `TZ`, `TMPDIR`, `USER`, `LOGNAME`, `SHELL`) plus
    `settings.sandbox_env_passthrough`, with `HOME` pinned to the workspace. Credentials
    (`ANTHROPIC_API_KEY`, `TAVILY_API_KEY`, `REDIS_URL`) are never visible to agent
    subprocesses. This is env scrubbing only — there is no OS-level sandbox, so full
    `bash_exec`/`python_exec` can still read files outside the workspace.
  - `bash_exec_readonly` (planner, decomposer, WriterAgent — all of which can also see
    untrusted web or workspace content) is validated by `_check_readonly_command()`
    before it reaches the shell: harmless stderr redirects (`2>&1`, `2>/dev/null`) are
    stripped; a quote-aware scan rejects unquoted `` ` $ ( ) { } [ ] ? < > ! `` and
    newlines, plus `$`/backticks inside double quotes (these expand before the command
    runs, e.g. `cat {..,x}/.env`); the rest is tokenised with `shlex` and split on
    `| || && ; &`. Every stage must start with a command in `_READONLY_COMMANDS` (no
    `env`, `printenv`, `xargs`, `awk`, `sed`), and every argument is checked by
    `_readonly_arg_error()`: `find -exec/-execdir/-ok/-delete/-fprint*`, `sort -o`,
    `tree -o` and `uniq IN OUT` are refused, as are `..` segments, `.*` globs and
    absolute paths (including flag-attached ones such as `--file=/x`) outside the
    workspace. Agents are told to single-quote search patterns.

  Key tools:
  - `arxiv_search` — searches arXiv Atom API; returns `title`, `abstract`, `url` (abs),
    and `pdf_url` (derived by replacing `/abs/` with `/pdf/` in the abs URL, normalised to
    https). Use `category=` to restrict to a subject area and avoid off-topic hits. The
    abstract is returned directly — agents should NOT call `fetch_url` on arxiv links.
  - `download_document` — fetches a PDF/text/markdown URL and saves it to `.downloads/`
    in the workspace; triggers KB ingest automatically via the `_kb_dispatch_fn` ContextVar
    if `KnowledgebaseAgent` is active in the run. Returns the workspace-relative path and
    the KB ingest outcome.
  - `fetch_url` — transparently redirects `arxiv.org/abs/` URLs to the Atom API so
    agents that accidentally call it on an arXiv link still get structured text. HTML
    responses (by `content-type` or a leading `<!doctype html`/`<html`) are converted to
    readable text by `html_to_text()` — a stdlib `HTMLParser` that drops
    script/style/nav/footer/form chrome, keeps light markdown for headings, list items
    and table cells, prefixes the `<title>`, and keeps only `<main>`/`<article>` content
    when present. Typically ~10x fewer chars than the raw page. JSON/plain text pass
    through unchanged. No
    longer self-truncates (`_truncate()`/`_MAX_CONTENT` were removed) — the generic
    result-size budget above handles it uniformly.
  - `file_read` — returns a `[from_line=X, to_line=Y, total_lines=Z]` header plus
    numbered lines, capped by both `settings.file_read_max_lines` (line count) and
    `settings.file_read_max_chars` (a backstop for files with pathologically long
    lines, e.g. minified code, that would otherwise blow past a reasonable response
    size while still under the line cap); the header says `use start_line=N to read
    more` when truncated by either cap, always keeping at least one line so a
    follow-up call makes forward progress. The `pattern` (regex) mode is capped by
    the same char budget and reports `shown X of Y matches` plus a suggestion to
    narrow the pattern when truncated.
  - `file_write` — multi-mode writer (`overwrite`, `append`, `replace_lines`, etc.).
    Artifacts recorded via `_record_artifact()` which writes to the `_current_sink`
    ContextVar set by the engine per run. `replace_lines`/`replace_between` echo a
    short (300-char) preview of the content via `_write_preview_note()`, explicitly
    labeled as a preview of content already written in full — never a bare
    slice-with-ellipsis, which reads to the model as an incomplete write and
    triggers a redundant retry (see the tool_use.input note above).
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
  `total_thinking_tokens`, `cache_creation_tokens`, `cache_read_tokens`). Also exports
  `estimate_thinking_tokens(content)`, the char-count estimate over `thinking` blocks
  shared with `agents/agent.py`'s per-call cost accounting (see above) so the two never
  drift apart. Internally
  uses `messages.stream()` / `beta.messages.stream()` + `get_final_message()` instead
  of `messages.create()` so that long agent turns are not cut off by the Anthropic SDK's
  10-minute non-streaming timeout. Rate limiting is delegated to the Anthropic SDK
  (`max_retries=4`, exponential backoff on 429/500) rather than a per-process limiter,
  which would not coordinate across replicas.

## Agent definition layer

- **`manifests/*.yaml` → `core/registry.py` — `AgentRegistry`**: `main.py` builds one
  registry at import time and calls `load_from_directory(settings.manifests_dir)`
  (default `manifests/`). Each `*.json`/`*.yaml`/`*.yml` file is validated into an
  `AgentManifest` (`core/models.py`). A file that fails validation is logged and skipped,
  not fatal, and a second file with the same stem is skipped as a duplicate.
  `OrchestratorEngine._build_agents()` then creates one generic `Agent` per manifest.
  Registry lookups in use: `get()`/`all()`; `summary()` renders the agent roster
  (domain, capabilities, tools including `<mcp-server>/*`, skills) that the planner
  injects as `Available Agents:`; and `find_fallback(agent_id)` returns the manifest whose
  `fallback_for` lists that id, which `_dispatch_subtask()` uses after the final retry fails.
  **Declared but not enforced:** `capabilities` feeds only the planner roster (there
  is a `by_capability()` index, but nothing calls it), and `AgentManifest.max_concurrency`
  is not read anywhere. Parallelism is limited only by the DAG.
- **`core/skill_loader.py` — `SkillLoader`** (global `skill_loader` over
  `settings.skills_dir`, default `skills/`): at the start of `Agent.run()`, every skill
  listed in `manifest.skills` is **pre-injected in full** into the cached system block via
  `full_content()`. That means `SKILL.md` plus every other file in the skill dir, not just a
  preamble. A skill therefore costs its whole size in (cached) input tokens on every
  agent of that type. The `read_skill` tool (`tools/skills.py`) remains available for
  on-demand reads, and names are validated (`[a-z0-9-]+`; topics disallow `..`). `preamble()`
  (the lighter "advertise skills, load on demand" mode) exists but is currently unused.
- **`tools/mcp_tools.py` — MCP adapter**: for each `manifest.mcp_servers` entry,
  `Agent.run()` enters `mcp_session(config)` on an `AsyncExitStack`. The session stays
  open for the whole subtask and closes when it ends. Its tools are wrapped as
  `ToolDefinition`s (names sanitized to `[a-zA-Z0-9_-]{1,128}`) and appended to the
  manifest's local tools. Transports: `sse` (default, `url`) or `stdio` (`command`/`args`).
  A stdio server does **not** inherit the API process's env. `_stdio_env()` returns only
  the vars the manifest opts into: `env_passthrough` names copied from the process env,
  then literal `env` values on top. The mcp SDK merges these onto its own safe default
  (`HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, `USER`), so a credential such as
  `ANTHROPIC_API_KEY` reaches a server only if its name is listed in `env_passthrough`.
  Unlike `_sandbox_env()`, the real `HOME` is kept so tools like `uv` find their caches.
  If the `mcp` package is missing, connectivity is disabled with a warning. The shipped
  `knowledgebase_agent` launches the `semantic-kb` server over stdio (`uv --directory
  workspace/semantic-kb run skb-mcp`).

## API surface & run persistence

`api/routes.py` (mounted at `/api`) only calls the engine, `stream_registry` and
`context_store`. Live-run endpoints:

| Endpoint | Behaviour |
| --- | --- |
| `POST /runs` | Start a run (see lifecycle step 1). |
| `GET /runs/{id}/stream` | SSE; `stream_registry.connect()` so any replica can serve it. |
| `POST /runs/{id}/input` | HITL response → `ctx.provide_human_input()`; 409 if nothing pending. |
| `POST /runs/{id}/cancel` | `engine.cancel_run()` cancels the run's asyncio Task; 404 if not active on **this** process. |
| `POST /runs/{id}/message` | `ctx.push_user_message()` fan-out (see Data flow); 409 once the emitter is done. |
| `POST /runs/{id}/followup` | Starts a **new** run whose `context` carries `prior_run_id`, `prior_task`, `prior_report` and `prior_subtask_outputs`, read from the old run's directory. The planner renders them as a "Prior Run" block; `_dispatch_subtask()` strips all four `prior_*` keys before building agent envelopes. |

Past-run endpoints (`GET /runs`, `/runs/{id}`, `/events`, `/results`, `/report`,
`/artifacts`, `/artifacts/{artifact_id}`) read the filesystem, not in-memory state.
Each run's directory is `<runs_dir>/<run_id>/` (`settings.runs_dir`, default `.runs`)
and holds `meta.json` (task, generated name, created_at), `events.jsonl` (when
`capture_events` is set), `results.jsonl`, `artifacts.jsonl` (appended by
`tools/artifact_tracker.py` as agents write files; paths are workspace-relative and
served with an `is_relative_to` containment check) and `report.md`. Because
`cancel_run()` looks only at the local `_run_tasks`, cancel is **not** cross-replica
under the Redis backend.

**Startup reconciliation**: the FastAPI `lifespan` in `main.py` calls
`engine.reconcile_orphaned_runs()`. Any run dir with `meta.json` but no `report.md`
counts as interrupted by a restart: a `run:error` event is appended to `events.jsonl`
and a "Run Interrupted" tombstone `report.md` is written. This assumes a single process
owns `runs_dir`. If several replicas share that directory, one replica starting up would
tombstone runs still in flight on the others.

The CLI (`cli/`, `agentflow run "<task>"`) is a thin click client over this HTTP API.
It posts to `/api/runs`, renders the SSE stream with Rich (`cli/display.py`) and stops on
`run:complete`/`run:error`/`run:cancelled`. It needs a running API server. Note
that `--port` defaults to 8000 (env `AGENTFLOW_PORT`), matching `uvicorn`'s default.

## Data flow & messaging

Within one run, components talk through three mechanisms:

- **Shared state — `core/context.py`**: `RunContext` is the source of truth for a run.
  `_dispatch_subtask()` writes each subtask's `AgentResult` via `ctx.store_result()`
  (optionally appended to `<runs_dir>/<run_id>/results.jsonl`); downstream subtasks read
  dependency output via `ctx.build_prior_results()` (combined prose + structured-JSON
  summaries keyed by dep task ID — both `output.text` and `output.structured` are
  included when present so the synthesizer sees the full agent output) and
  `ctx.build_upstream_artifacts()` (dict of dep task ID → `files_written` list from
  that task's `AgentResult`). Both are thin wrappers over the backend-agnostic helpers in
  `core/prior_results.py`, which the Redis context also calls — so the two backends
  cannot drift in what downstream agents see. The agent receives both as an `<upstream_context>` block
  in its initial user message — it reads the exact files it needs rather than receiving
  the full upstream conversation history. The planner's system prompt ("Context
  inheritance") states this explicitly so it tells downstream agents to read upstream
  files by path rather than claiming their content is "already in context". `RunContext`
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
- [`docs/context-optimization-plan.md`](../context-optimization-plan.md) — the fix
  plan this result-size budget implements (Fix 1), plus unimplemented follow-ons
  (upstream-context pointer injection, in-loop history trimming, real auto-compact).
