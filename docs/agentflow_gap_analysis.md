# AgentFlow: Gap Analysis Against Scalable Multi-Agent Systems Research

> Validated against: *Implementing Scalable Multi-Agent AI Systems with Human-in-the-Loop Support*
> (`docs/scalable_multi_agent_ai_systems.md`)
>
> Mapped across the six dimensions identified as load-bearing for production systems:
> architecture topology, concurrency strategy, HITL integration, state management,
> framework selection, and operational excellence.

---

## What AgentFlow Does Well

Before the gaps: the foundation is solid. The codebase has a clean orchestrator/worker topology (DAG scheduler, parallel dispatch via `asyncio.gather`), declarative YAML manifests, prompt caching, fallback agent routing, continuation loops for partial results, a typed SSE event stream, and budget-aware execution. These are non-trivial and the right instincts.

The issues are not fundamental — they are about what the system cannot yet survive: a server restart, a second server, a thousand users, a compromised tool output, or a human reviewer who needs to do more than approve a budget.

---

## Gap 1 — State Is Process-Local (Critical Scalability Blocker)

**Affected files:** `src/agentflow/core/context.py`, `src/agentflow/orchestrator/stream.py`, `src/agentflow/core/bus.py`

**The problem:** `ContextStore`, `StreamRegistry`, and `TaskBus` are Python-process singletons (`dict` in memory). The research calls this the "critical anti-pattern": a stateful in-process orchestrator with no external state store. A single server restart, deploy, or OOM kill silently destroys every in-flight run. You cannot run two replicas behind a load balancer — they would each have an empty `ContextStore`, so any request hitting a different replica than the one that created the run would 404.

**Research prescription:** stateless orchestrator + Redis hot tier (session state, graph checkpoints, pending HITL interrupts) + Postgres warm tier (task history, audit logs). `TaskBus` already has a comment noting Redis-swappability — the interface is right, the implementation is missing.

### Proposals

**P1.1 — Redis-backed `RunContext`**
Replace `_results: dict` with a Redis hash (`run:{run_id}:results`). Replace `asyncio.Event` for HITL signaling with a Redis pub/sub channel. `RunContext` becomes a thin wrapper that reads/writes to Redis, making any replica able to resume any run.

**P1.2 — Redis-backed `StreamEmitter`**
Replace the per-process `asyncio.Queue` with a Redis Stream (`XADD run:{run_id}:events`). The SSE endpoint becomes a Redis consumer (`XREAD BLOCK`) — decoupled from whichever process is producing events.

**P1.3 — Redis-backed `TaskBus`**
Implement the Redis path that is already noted in `bus.py`. Use `LPUSH`/`BRPOPLPUSH` for task dispatch with a reliable delivery pattern for result publishing. This is the concrete step that unlocks worker-pool horizontal scaling.

**P1.4 — Postgres for durable run records**
Current disk persistence (`results.jsonl`, `events.jsonl`) works fine for single-server development but is not queryable and breaks under multi-server deployments. Move run metadata, subtask results, and HITL audit events to a Postgres table. Keep file artifacts on disk or object storage.

---

## Gap 2 — HITL Is a Budget Gate, Not a Safety Control

**Affected files:** `src/agentflow/orchestrator/engine.py` (`_await_human_input`), `src/agentflow/core/models.py` (`HumanInputRequest`)

**The problem:** The only HITL trigger is budget exhaustion. Agents can call `bash_exec`, `file_write`, `fetch_url`, or any MCP tool with no human approval gate. Per the research decision tree: *Is the action irreversible? → REQUIRE approval gate.* `bash_exec` and `file_write` are irreversible by definition.

Worse: when human input *is* requested, `HumanInputRequest` exposes only the budget number. Wang & Wang (2026) found that agents given only output-level human feedback achieved **0% task completion** due to "failure mode oscillation". The HITL payload must include the reasoning trace, tool call history, and sub-goal completions — not just the trigger condition.

### Proposals

**P2.1 — General-purpose interrupt mechanism**
Introduce an `InterruptRequest` model with:

```python
{
  "interrupt_id": str,
  "run_id": str,
  "subtask_id": str,
  "trigger_reason": Literal["irreversible_action", "low_confidence", "budget_exceeded", "scope_exceeded", "explicit_request"],
  "proposed_action": {"tool": str, "inputs": dict},
  "reasoning_trace": [{"role": str, "name": str, "input": dict, "output": str}, ...],
  "options": ["approve", "reject", "modify"],
  "expires_at": datetime
}
```

Map to a new endpoint: `POST /runs/{run_id}/interrupts/{interrupt_id}/respond` with `InterruptResponse` allowing: `approve`, `reject`, `modify` (with modified action params), `restart_from_step`.

**P2.2 — Tool-impact gate in the agentic loop**
In `src/agentflow/agents/agent.py` `_call_tool()`: before executing any tool with `impact == ToolImpact.execute` or `ToolImpact.write`, check if the agent manifest declares `require_approval: ["bash_exec", "file_write"]`. If so, emit an `InterruptRequest` and suspend the agentic loop until the human responds. Resume with the approved/modified/rejected decision injected back into the message thread.

**P2.3 — Rich HITL payload (Observability Gap compliance)**
When emitting any interrupt, the payload must include the full reasoning trace, tool results, iteration count, cost so far, and the proposed action. Per Wang & Wang (2026), this is not optional — it is what makes human decisions causally effective. Humans cannot effectively correct what they cannot observe.

**P2.4 — SLA-bounded timeouts with safe defaults**
Each `InterruptRequest` should carry `timeout_seconds` and `safe_default` (`approve` for low-impact, `reject` for high-impact). The orchestrator sets a timer; on expiry, applies the safe default and logs the escalation. Currently `human_input_timeout_s = 1800` but there is no safe default on timeout — the run hangs indefinitely.

---

## Gap 3 — No Auth, No Tenant Isolation

**Affected files:** `src/agentflow/api/routes.py`

**The problem:** Every endpoint is unauthenticated. Any client who guesses or observes a `run_id` can read results, stream events, read artifacts, or inject a HITL response. There is no `tenant_id` concept. The research is explicit: "multi-tenant deployments must enforce strict data boundaries; Redis keyspace segregation and Postgres RLS are non-negotiable."

### Proposals

**P3.1 — API key authentication middleware**
FastAPI dependency that validates `Authorization: Bearer <key>` against a config-backed or DB-backed key store. Attach `tenant_id` to the request context. Single-tenant deployments use one key; multi-tenant adds per-key scoping.

**P3.2 — `tenant_id` namespacing throughout**
Prefix every Redis key: `{tenant_id}:run:{run_id}:*`. Scope all file paths: `.runs/{tenant_id}/{run_id}/`. Add `tenant_id` to `RunContext`, `RunInfo`, and all API routes. A `run_id` lookup must verify the tenant owns the run before returning data.

**P3.3 — Prompt injection sanitization**
In `src/agentflow/tools/builtin.py`, after every tool fetch (web content, file read, search results), run a lightweight sanitizer that strips or escapes patterns matching `<SYSTEM>`, `Ignore previous instructions`, `[INST]`, etc. before injecting into the tool result message. This is the primary attack surface identified by the research.

---

## Gap 4 — No Backpressure or Request Queue

**Affected files:** `src/agentflow/api/routes.py` (`create_run` → `BackgroundTasks.add_task`)

**The problem:** Every submitted run spawns a `BackgroundTask` immediately. Under high concurrency this creates unbounded asyncio task accumulation — all competing for the event loop, LLM rate limits, and RAM. There is no `HTTP 429` response, no queue depth metric, no mechanism to shed load. The research recommends responding with `HTTP 429 Too Many Requests + Retry-After` when overloaded rather than silently queuing indefinitely.

### Proposals

**P4.1 — Concurrency semaphore**
Add `settings.max_concurrent_runs: int = 20`. In `create_run`, acquire an `asyncio.Semaphore` before spawning the background task; if full, return `HTTP 429` with `Retry-After: 30`. This is a minimal change that prevents the most common overload pattern.

**P4.2 — Task queue backend (production path)**
For workloads where runs outlive a single request cycle, replace `BackgroundTasks` with a Celery job: `POST /runs` enqueues a Celery task and returns `run_id` immediately. Workers pull from Redis/RabbitMQ. Celery's `chord` and `group` primitives map directly onto the DAG fan-out/fan-in pattern already in the engine. This also naturally resolves the process-local state problem (Gap 1) because Celery workers share the Redis state store.

---

## Gap 5 — No Distributed Tracing or LLM Observability

**Affected files:** `src/agentflow/logging_config.py`, `src/agentflow/llm/client.py`

**The problem:** Structured logging and SSE events exist, but there is no distributed trace spanning `POST /runs` → planner → subtask dispatch → agent LLM call → tool execution → result aggregation. There are no Prometheus metrics. Wang & Wang (2026): "you cannot improve what you cannot see, and what you cannot see in multi-agent systems is the intermediate reasoning state."

### Proposals

**P5.1 — OpenTelemetry instrumentation**
Add `opentelemetry-sdk`, `opentelemetry-instrumentation-fastapi`, `opentelemetry-instrumentation-httpx`. Initialize a `TracerProvider` in `main.py` exporting to Jaeger or an OTLP collector. Wrap:
- `OrchestratorEngine.run()` → root span with `run_id`, `task`, `budget_usd`
- `Agent._execute()` → child span with `agent_id`, `subtask_id`
- `LLMClient.call()` → leaf span with model, token counts, latency
- Tool calls → leaf spans with tool name and impact level

**P5.2 — Prometheus `/metrics` endpoint**
Expose via `prometheus-fastapi-instrumentator`:
- `agentflow_active_runs` gauge
- `agentflow_llm_requests_total` counter (by model, status)
- `agentflow_llm_latency_seconds` histogram (by model)
- `agentflow_tokens_total` counter (by type: input/output/cache_read/cache_write)
- `agentflow_cost_usd_total` counter (by agent_id)
- `agentflow_task_duration_seconds` histogram (by agent_id, status)

**P5.3 — LangSmith / Langfuse LLM trace capture**
Integrate LangSmith's `traceable` decorator on `LLMClient.call()` to capture every prompt, completion, and tool sequence. Enables: side-by-side run comparison, annotation queues, RLHF export, and regression detection on deployment. Langfuse is a self-hosted alternative that avoids sending data to a third party.

---

## Gap 6 — Context Window Management Is Uncontrolled

**Affected files:** `src/agentflow/core/context.py` (`build_prior_results`, `build_prior_messages`)

**The problem:** `build_prior_results` concatenates all upstream text outputs as a raw JSON blob. `build_prior_messages` passes the full message list (all tool calls + results) from a dependency. For long-running tasks with deep dependencies this can balloon into tens of thousands of tokens per call — a silent cost and latency driver.

Haseeb (2025): "structured Pydantic state objects instead of raw message history can reduce per-call token usage by 60–80% for long sessions." Targeted context injection per agent role significantly outperforms passing undifferentiated full context to every agent.

### Proposals

**P6.1 — Structured state object per agent role**
Define an `AgentContextSlice` Pydantic model per domain (e.g., `CodeContext`, `ResearchContext`, `ReviewContext`). The orchestrator builds a typed slice for each downstream agent from upstream results, rather than dumping raw output. Include only fields the agent's role needs.

**P6.2 — Configurable `prior_messages` truncation**
In `build_prior_messages`, add a `max_messages: int` parameter (default 20) and a `summarize_beyond: bool` flag. When the message thread exceeds `max_messages`, call a cheap model (Haiku) to produce a summary, then pass `[summary_message] + last_N_messages`. The summarizer call is cheap; the savings on the main agent call are large.

**P6.3 — Semantic result deduplication**
Before appending `prior_results`, compute a hash of each result's key semantic content. If a subtask produces output that is ≥90% similar to an already-stored result in the run context (via lightweight token-overlap check), mark it as a cache hit and skip injection. This is especially relevant when multiple research subtasks query overlapping sources.

---

## Gap 7 — No Model Routing

**Affected files:** `src/agentflow/config.py`, `src/agentflow/agents/agent.py`

**The problem:** `agent_model` is a single global config. Every subtask — whether a trivial extraction or a complex multi-step code generation — uses the same Sonnet model. The research identifies model routing as a high-leverage latency and cost control: route simpler subtasks to fast, cheap models; reserve large models for high-complexity decisions.

### Proposals

**P7.1 — Per-manifest model override**
Add `model: Optional[str]` to `AgentManifest`. If set, the agent uses that model regardless of the global default. This allows a `kb_search_agent` to run on Haiku while a `code_agent` runs on Sonnet — with zero change to orchestration logic.

**P7.2 — Complexity-based model routing in planner**
Add `complexity_tier: Literal["low", "medium", "high"]` to `Subtask`. The planner scores complexity during decomposition. `_dispatch_subtask` selects the model from a config map:

```python
MODEL_ROUTING = {
    "low": "claude-haiku-4-5-20251001",
    "medium": "claude-sonnet-4-6",
    "high": "claude-opus-4-7"
}
```

A reasoning agent always gets high-tier; a formatting or extraction agent gets low-tier.

---

## Gap 8 — No Cross-Session Shared Memory

**Affected files:** `src/agentflow/core/context.py`

**The problem:** Every run starts from a blank slate. If agent A in run #1 researched a topic and produced a thorough answer, agent A in run #2 on the same topic re-does all the work. The research describes three shared-memory patterns — shared vector store, blackboard architecture, event sourcing — none of which are implemented.

### Proposals

**P8.1 — Shared result cache (blackboard lite)**
Add a Redis hash `shared:results:{capability}:{content_hash}` where `content_hash` is a hash of the instruction. Before dispatching a subtask, check for a recent result (< TTL) for the same instruction + agent_id. On cache hit, return the cached result and emit `task:cache_hit` event. Lowest-implementation-cost path to cross-session memory.

**P8.2 — Agent knowledge store (vector memory)**
Add a `pgvector` table (or Pinecone index) for agent-generated artifacts. After a run completes, embed key outputs and store them with `{run_id, agent_id, capability, embedding, summary}`. Future runs retrieve relevant prior work via semantic search before dispatching subtasks — the agent receives retrieved summaries as additional context.

---

## Gap 9 — Testing Coverage Is Incomplete

**Affected files:** `tests/`

**The problem:** Tests cover unit-level (data models, DAG logic, tool registry) with mocked LLM responses. Per the research testing matrix: integration tests (real LLM, real state store), HITL simulation, load tests, chaos tests, and adversarial tests are all absent.

### Proposals

**P9.1 — HITL simulation test**
Write a pytest fixture that intercepts `InterruptRequest` events and programmatically responds via the HTTP endpoint. Test scenarios: (a) approve → execution continues, (b) reject → run terminates cleanly, (c) modify → modified action is executed, (d) timeout → safe default applied.

**P9.2 — Integration test with real LLM (small model)**
One integration test suite that runs a minimal 2-agent DAG against the real Anthropic API using `claude-haiku-4-5` (cheap). Validates: plan creation, subtask dispatch, result aggregation, SSE event sequence. Run in CI with a daily budget cap via a separate API key.

**P9.3 — Prompt injection test corpus**
A pytest parametrize suite feeding known injection patterns (`<SYSTEM>Ignore all previous...`, `\n\nHuman: now do X`) through every built-in tool (web content, file read, search results) and asserting the agent does not follow injected instructions. Catches sanitizer regressions.

---

## Prioritized Roadmap

| Priority | Gap | Effort | Impact |
|---|---|---|---|
| **P0** | P1.1–P1.3 — Redis-backed state (context, stream, bus) | 2–3 days | Unlock horizontal scaling; survive restarts |
| **P0** | P2.1–P2.3 — General HITL interrupt + rich payload | 2 days | Safety for irreversible actions; HITL effectiveness |
| **P1** | P3.1–P3.2 — API auth + tenant namespacing | 1 day | Security prerequisite for any user-facing deployment |
| **P1** | P4.1 — Concurrency semaphore + HTTP 429 | 2 hours | Prevents overload collapse |
| **P1** | P5.1–P5.2 — OpenTelemetry + Prometheus | 1–2 days | Operational visibility; HITL observability compliance |
| **P2** | P6.1–P6.2 — Context slicing + truncation | 1–2 days | 40–80% token reduction on long chains |
| **P2** | P7.1–P7.2 — Per-manifest model + complexity routing | 1 day | 3–5× cost reduction on low-complexity subtasks |
| **P3** | P1.4 — Postgres warm tier | 2 days | Queryable history; audit logs |
| **P3** | P8.1 — Shared result cache | 1 day | Cross-session efficiency |
| **P3** | P5.3 — LangSmith/Langfuse LLM tracing | 1 day | Deep LLM observability |
| **P3** | P4.2 — Celery task queue backend | 3–4 days | Long-horizon run durability |
| **P4** | P8.2 — Vector memory store | 3 days | Cross-session knowledge reuse |
| **P4** | P9.1–P9.3 — Testing: HITL sim, integration, adversarial | 2–3 days | Regression safety net |
| **P4** | P3.3 — Prompt injection sanitization | 1 day | Defense-in-depth |

---

## Architectural North Star

The single sentence that summarizes what a P0+P1 sprint buys: **a stateless orchestrator that can survive a server restart and run behind a load balancer, with human approval gates on irreversible actions that show reviewers the reasoning trace, not just the action.** Everything else is incremental quality. Those two properties are what separates a prototype from a production system per the research.

---

*Validated against: Luo & Shao (2026), Wang & Wang (2026), Zou et al. (2025), Haseeb (2025), Tripathy et al. (2025), Goyal et al. (2024).*
