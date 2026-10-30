# AgentFlow — Presenter Notes

*30–45 minute presentation. ~90 seconds per slide.*

---

## Slide 1 — Title

Today I'm walking through AgentFlow: a multi-agent AI orchestration platform built on the Claude API.

The premise: complex knowledge work requires more than a single LLM call. It requires coordinating specialists, managing parallelism, tracking cost in real time, enforcing safety, and streaming results live. That's what AgentFlow does.

Four areas: the problem and business value; architecture and six design decisions; key implementation patterns; and three deployment scenarios from dev server to cloud-native.

---

## Slide 2 — The Problem

A single LLM breaks down on tasks requiring depth across multiple domains — context windows overflow, there's no parallelism, no audit trail, and no cost visibility.

Think about how a senior analyst actually works: they divide the work, run in parallel, integrate results. We need AI systems that do the same.

The numbers on the right are real. Parallelism cuts wall-clock time up to ten times. Prompt caching reduces repeated token costs up to eighty percent. Model routing reduces overall cost three to five times. These come directly from the architectural choices we'll cover.

---

## Slide 3 — What Is AgentFlow?

An orchestration engine: submit a task with a budget cap, AgentFlow decomposes it into a graph of specialized subtasks, routes each to the right agent, runs them in parallel where possible, and streams every state transition live.

The six properties — composable, parallel, transparent, budget-safe, tool-rich, resilient — are concrete engineering decisions, not marketing:

- **Composable**: new agents via YAML, no orchestration code changes
- **Parallel**: DAG scheduler dispatches all independent subtasks simultaneously
- **Transparent**: every state change streams immediately — no polling, no blind waits
- **Budget-safe**: per-subtask USD cap enforced in real time, with a human gate on overrun
- **Tool-rich**: built-in tools plus any external tool via MCP
- **Resilient**: automatic retry, fallback routing, continuation from partial results

---

## Slide 4 — Business Value

**Speed**: parallel agents reduce complex tasks from thirty minutes to five. That changes what's economically viable to automate.

**Cost predictability**: declared budget caps, prompt caching cuts repeated costs up to ninety percent, model routing reduces overall AI spend three to five times.

**Human oversight**: HITL is a first-class architectural concept, not an afterthought. Budget-exceeding actions require approval; the roadmap extends this to any irreversible action. Compliance teams get a complete audit log.

**Extensibility**: new domain, new agent — one YAML file, no orchestrator redeployment, no risk to existing workflows. Hours, not sprints.

---

## Slide 5 — Section Break: Architecture

Shifting into architecture. Key questions to hold: how does the system decompose a task, how does it run agents without collisions, and how does it prevent things from going wrong?

---

## Slide 6 — System Architecture

Five layers:

**Client** — submits a task, connects to an SSE stream, receives everything pushed in real time. No polling.

**API layer** — FastAPI, deliberately thin: validate, create a run record, fire a background task, return a run ID in milliseconds.

**Orchestration engine** — three internal components: Planner (LLM call that decomposes the task), Scheduler (builds and executes the dependency DAG), Stream Emitter (publishes typed events as the run advances).

**Agent layer** — one Agent class. Every agent is the same class loaded with a different manifest. Behavior is configuration, not code.

**Core layer** — Registry, RunContext, TaskBus. Clean interfaces designed to swap for Redis-backed implementations when scaling horizontally.

**Tools and storage** — scoped per agent. Local disk today, object storage on the cloud path.

---

## Slide 7 — Six Design Principles

1. **Declarative agents**: YAML manifest fully describes an agent. Adding a domain is a config change, not a code change.

2. **Stateless agents**: agents receive a typed task envelope — instruction, budget, upstream results. They return a result. No shared memory, no direct agent-to-agent communication. Trivially parallelizable.

3. **LLM-powered planning**: decomposition is itself an LLM call. As you add agents to the registry, the planner automatically learns to use them.

4. **Streaming-first**: every state change emits a typed event immediately. No batch delivery, no polling.

5. **Budget awareness**: USD cost tracked after every API call. Subtask that exceeds its allocation stops and asks.

6. **Swappable internals**: TaskBus, StreamEmitter, RunContext implement clean interfaces. In-process today, Redis-backed in production. Nothing else changes.

---

## Slide 8 — Declarative Agent Manifests

The YAML for ResearchAgent makes the concept concrete:

- `agent_id`: how the planner refers to this agent
- `capabilities`: how the planner knows to assign research subtasks here
- `tools`: exact tools this agent can call — the registry filters the full catalog to only these
- `mcp_servers`: external tools connect here; agent sees built-ins and external tools as a unified list
- `skills`: shared knowledge libraries injected at runtime
- `max_iterations`: safety cap on the agentic loop
- `fallback_for`: if the primary agent fails, the orchestrator finds the fallback here

The critical implication: a domain expert with no Python knowledge can write this file and deploy a new AI capability.

---

## Slide 9 — Orchestration Pipeline

Seven steps per task:

1. **Initialization** — run directory, stream emitter, task bus created; `run:started` emitted
2. **Planning** — LLM planner explores workspace, outputs structured JSON execution plan
3. **Expansion** — agents that declare `decomposition_prompt` further split their subtask into micro-tasks
4. **Scheduling** — builds the DAG, topological sort, dispatches all zero-dependency subtasks simultaneously
5. **Execution** — agents run agentic loops, accumulate results, emit progress events
6. **Continuation** — subtasks that hit iteration or budget limits return `partial`; engine can re-dispatch with a continuation prompt up to three times
7. **Reporting** — reporter agent compiles the final markdown, emits `run:complete`

Retry and fallback sit across steps four through six: configurable retries with exponential backoff, then `fallback_for` registry lookup on final failure.

---

## Slide 10 — LLM-Powered Planning

Most frameworks hardcode routing logic. AgentFlow's planner is an LLM call that receives the complete agent registry and decides how to decompose the task.

**Phase 1 — workspace exploration**: the planner runs a ReAct loop with `file_read`, `bash_exec`, and `web_search` — up to eight tool calls — to understand the workspace before committing to a plan. Prevents plans that ignore existing context.

**Phase 2 — decomposition**: structured JSON output. Each subtask declares: agent, instruction, dependencies, budget fraction. Budget fractions must sum to 1.0. Hard rules enforced in the system prompt: prefer parallel dependencies, add verification after file generation, never assign to an agent without the required capability.

Example: `st_1` and `st_2` have no `dependsOn` — they run in parallel. `st_3` depends on both — runs after and receives their outputs.

Key property: add agents to the registry, and the planner uses them automatically.

---

## Slide 11 — DAG Dependency Scheduling

The scheduler converts the planner's output into a runtime dependency graph using NetworkX.

Each subtask is a node; each `dependsOn` declaration is a directed edge. Cycle detection runs at plan ingestion — circular dependencies fail fast with a descriptive error, not a silent runtime deadlock.

Each loop iteration computes `ready(completed, failed)` — subtasks whose entire dependency set is complete — and dispatches them all via `asyncio.gather()`.

**The parallelism dividend**: if Research takes sixty seconds and Data takes forty-five, serial execution means Writer starts at one hundred five seconds. Parallel execution means Writer starts at sixty seconds — the duration of the longer subtask. No extra effort from agents or the planner.

**Context propagation at fan-in**: multiple dependencies → structured JSON summary of each upstream output. Single dependency → full message thread including tool calls and reasoning.

---

## Slide 12 — Section Break: Implementation

Five implementation patterns: the agentic loop, real-time streaming, prompt caching, human-in-the-loop, and skills. Then context propagation and model routing.

---

## Slide 13 — The Agentic Loop

Standard ReAct pattern: Think, Act, Observe, repeat.

Each iteration: call Claude with the cached system prompt, accumulated message history, and scoped tool definitions. Response is either `end_turn` or `tool_use`.

On `tool_use`: call the tools, collect results — capped at eight thousand characters each — append to the message thread, loop.

On `end_turn`: parse the final message, extract structured JSON, return `AgentResult` with status `success`.

Three stop conditions:
- `end_turn` → success
- Budget exhausted → `partial`
- Max iterations → `partial`

Partial results are first-class: stored in run context, passed to downstream agents as context, re-dispatchable with a continuation prompt up to three times.

Cost tracked after every API call — not after the subtask completes. That's what makes budget enforcement precise.

---

## Slide 14 — Real-Time Streaming

Protocol: Server-Sent Events — standard HTTP server-to-client push. No polling, no WebSocket complexity.

The client opens one long-lived connection to `GET /runs/{id}/stream`. Inside the engine, each run owns a `StreamEmitter` with an `asyncio.Queue`. The orchestrator calls `emitter.emit(event_type, data)`, which queues a serialized event. FastAPI's `EventSourceResponse` handles the wire framing.

Deterministic event sequence: `run:started` → `plan:created` → waves of `task:dispatched` and `agent:progress` → `task:complete`/`task:failed` per subtask → `run:complete`. Budget gate fires a `human_input_request` and pauses the stream.

All events carry a sequence number and are persisted to JSONL. A disconnected client can reconnect and replay the log to reconstruct full run state.

The `asyncio.Queue` is designed to be replaced. In horizontally scalable deployment: producers call `XADD`, consumers call `XREAD BLOCK` on a Redis Stream. The SSE endpoint doesn't change.

---

## Slide 15 — Prompt Caching

The highest-leverage cost optimization available when building with Claude.

Mechanism: mark input blocks with `cache_control`. Claude stores the key-value attention state. Subsequent calls within a five-minute window that include identical blocks hit the cache — at one tenth the cost.

Pricing: standard input is three dollars per million tokens. Cache reads are thirty cents per million. Ten times cheaper.

AgentFlow caches: agent system prompts, tool definition schemas, skill content, and prior message history on continuation runs.

Example: five-hundred-token system prompt, eight iterations. Without caching: four thousand tokens of system prompt cost. With caching: five hundred as a cache write, then seven reads at one tenth the price. System prompt cost drops eighty-seven percent.

At organizational scale — hundreds of runs per day against shared agents — this compounds. Overall LLM costs can drop forty percent or more from caching alone.

---

## Slide 16 — Human-in-the-Loop

Today, one HITL trigger: budget exhaustion.

When a subtask consumes its USD allocation, the orchestrator emits `human_input_request`, acquires an async lock — so only one approval surfaces at a time — and waits up to thirty minutes for a response via `POST /runs/{id}/input`. Approve with new budget → subtask resumes. Reject → subtask marked partial, run continues. Full interaction logged to JSONL.

But budget exhaustion is only the beginning. `bash_exec` is irreversible. `file_write` is irreversible. A web POST is irreversible. These actions should require approval gates before execution.

The roadmap extends the interrupt mechanism to any high-impact tool action. And critically — when we ask a human to approve, they must see the full reasoning trace: what the agent was trying to do, what it already tried, and exactly what it's proposing. Wang and Wang (2026): agents given only output-level human feedback — just a thumbs up or down — achieved zero percent task completion. The human must see the reasoning.

---

## Slide 17 — Skills

Skills are shared domain knowledge without repeating it in every agent's system prompt.

A skill is a markdown file — coding standards, research methodology, domain conventions, step-by-step workflows. When an agent's manifest declares a skill, the engine injects a `read_skill` tool and instructs the agent to load it before beginning work. The agent reads the file on its first tool call; from that point the content lives in its context.

Because skill text is static, it's an ideal cache candidate — first call writes, every subsequent loop reads at one tenth the cost.

The organizational implication: your engineering standards, research methodology, documentation conventions live in one file. When standards change, update one file and every future run reflects it automatically.

Skills are where valuable institutional knowledge gets encoded. A skilled ResearchAgent follows your team's sourcing standards, citation practices, and quality criteria — not a generic LLM's defaults.

---

## Slide 18 — MCP Tool Integration

MCP is an open standard — created at Anthropic, now broadly adopted — for exposing tools to language models consistently.

Every built-in tool — bash execution, file read/write, web search, URL fetch — is an MCP tool with a formal schema. Agents declare exactly which tools they need in their manifest; the registry enforces it. An undeclared tool is uncallable, even if it exists globally.

External tools: your knowledge base, database query interface, Jira connector, GitHub integration — exposed as an MCP server. The manifest declares the server address and transport; the engine connects at startup, pulls tool schemas, and presents them alongside built-ins transparently. The agent sees one unified tool list.

Two transports: stdio (spawns a child process — good for local tools) and SSE (connects to a running HTTP service — good for shared organizational tools).

Plugging AgentFlow into your existing tool ecosystem is a configuration exercise, not a development project.

---

## Slide 19 — Context Propagation

Naively dumping all upstream results into every downstream agent's context breaks quickly — six thousand tokens of prior output before the agent even sees its instruction.

AgentFlow uses two propagation modes based on the shape of the dependency graph:

**Multi-dependency** (`build_prior_results()`): synthesizes a structured JSON summary of each upstream agent's output. Clean, organized, efficient.

**Single-dependency** (`build_prior_messages()`): passes the complete conversation thread — tool calls, tool results, model reasoning. The downstream agent sees not just the conclusion but the reasoning behind it. Essential when agent B needs to continue where agent A left off.

The user's original context dictionary — passed in the initial run request — propagates to every agent via the task envelope. Declare it once; every agent in the entire DAG has it.

---

## Slide 20 — Model Routing

Not every subtask needs the same model. Running everything on a high-end model is the AI equivalent of hiring senior engineers to file paperwork.

Today: per-manifest model overrides. The reporter agent runs on Haiku. Research, code, data, and writing agents run on Sonnet. The planner runs on Sonnet — decomposition requires genuine reasoning.

Roadmap: complexity-based routing at the planner level. Each subtask gets a complexity tier — low, medium, high. Engine selects from a routing table: Haiku for low, Sonnet for medium, Opus for high.

The cost impact: Haiku costs approximately twenty times less per input token than Opus. If forty percent of subtasks are low complexity — and in practice many are — routing them to Haiku reduces total LLM spend by thirty-five percent or more with negligible quality impact.

---

## Slide 21 — End-to-End Flow

Client posts a task with a three-dollar budget. Run ID returned in milliseconds. Client connects to SSE.

`run:started` → planner explores workspace (eight to fifteen seconds) → `plan:created` with three subtasks: ResearchAgent, DataAgent, WriterAgent.

Research and Data have no dependencies — dispatched simultaneously. `agent:progress` events stream from both in parallel.

Both complete → `task:complete` × 2 → Writer dispatched immediately (both dependencies satisfied).

Writer receives synthesized upstream outputs, produces the final report → `task:complete`.

Orchestrator compiles report → `run:complete` with full text, per-subtask cost breakdown, and token counts.

Total wall-clock: sixty to one hundred seventy-five seconds. Serial equivalent: one hundred eighty to three hundred ninety seconds. That's the parallelism dividend in actual time.

---

## Slide 22 — API Design

Minimal, REST-standard. No proprietary SDKs required.

**Run lifecycle**:
- `POST /runs` — submit task, budget, optional context; returns run ID immediately
- `GET /runs/{id}/stream` — SSE stream, consumable by any `EventSource` or SSE-capable client
- `POST /runs/{id}/input` — submit a human response to a HITL gate

**Result retrieval**: compiled report, per-subtask results, replayable event log, artifacts, run metadata.

The complete integration is fifteen lines: submit the run, open an EventSource, listen for `run:complete`, handle HITL if it fires.

The complexity lives entirely inside AgentFlow. Your integration is that fifteen-line example.

---

## Slide 23 — Section Break: Deployment

Three scenarios in increasing order of sophistication: single server (where the system runs today), horizontally scalable with Redis, and cloud-native with Kubernetes and Celery. Same codebase in all three — the difference is in the backing implementations of the core interfaces.

---

## Slide 24 — Single-Server Deployment

Minimal stack: Python with uv, FastAPI under uvicorn, in-process asyncio, local disk. Only external dependency: an Anthropic API key. Four commands to start.

Appropriate for: development, experimentation, low-concurrency internal tooling, demonstrations. Handles approximately twenty concurrent runs.

Limitations worth naming directly:
- State is process-local — a server restart drops all in-flight runs
- Cannot run two replicas — each would have a separate RunContext
- No authentication
- No backpressure — accepts all submissions immediately

These are not fundamental flaws. They're the cost of starting simple. The core interfaces were designed from day one to be swappable. The path to multi-replica is targeted implementation work, not an architectural rethink.

---

## Slide 25 — Horizontally Scalable Deployment

Key insight: to run multiple API replicas, remove all state from the process.

RunContext, StreamEmitter, and TaskBus become Redis-backed. Same interfaces — any replica can handle any request. A run created on Replica 1 can be streamed by Replica 2. A restart loses nothing.

Implementation changes are targeted:
- RunContext → Redis hash per run (`run:{id}:results`)
- HITL signaling → Redis pub/sub instead of asyncio event
- StreamEmitter queue → Redis Stream (`XADD` / `XREAD BLOCK`)
- TaskBus → `LPUSH` / `BRPOPLPUSH` for reliable delivery

Postgres joins for durable, queryable run history and compliance audit trails.

Authentication becomes mandatory: API key middleware attaches a tenant ID; all keys and file paths prefixed with it.

Backpressure: concurrency semaphore limits active runs; excess receives HTTP 429 with Retry-After.

---

## Slide 26 — Cloud-Native Deployment

For high-volume production and long-horizon runs: decouple HTTP request handling from AI execution.

**API pods**: truly stateless. Validate requests, enqueue Celery tasks, serve SSE as Redis consumers. No LLM work. Scale on requests-per-second, can autoscale to near-zero.

**Celery task queue** (Redis or RabbitMQ): `POST /runs` enqueues a task and returns a run ID in milliseconds. The HTTP request is done.

**Worker pods**: pull tasks, run the full orchestration engine. Autoscale on queue depth. A forty-five-minute run runs on a worker regardless of client connection state.

This decoupling is critical for durability: the client doesn't need an open connection for the duration of a long run.

Celery's `chord` and `group` primitives map naturally onto AgentFlow's DAG fan-out — the same pattern, now across worker processes.

Observability stack: OpenTelemetry, Prometheus, Langfuse.

---

## Slide 27 — Observability

Three layers:

**Distributed tracing (OpenTelemetry)**: trace tree rooted at `OrchestratorEngine.run()`. Child spans per planner and agent dispatch. Leaf spans per LLM call (timing, token counts, cost) and per tool execution. When something produces wrong output or unexpected cost, you can pinpoint the exact operation.

**Prometheus metrics**: operational dials — active runs, LLM latency distribution per model, total cost per agent per day, prompt cache hit rate. Into Grafana dashboards for on-call monitoring.

**LLM trace capture (Langfuse or LangSmith)**: where OpenTelemetry tells you *that* a call happened, Langfuse captures *what* happened — full prompt, full completion, every tool call and result. Enables side-by-side quality comparison, prompt optimization, regression detection after model upgrades, and building supervised datasets from successful runs.

You cannot improve what you cannot see. In multi-agent systems, what's hardest to see is intermediate reasoning state.

---

## Slide 28 — Gap Analysis Summary

Nine gap categories identified against peer-reviewed research. Critical ones:

**P0 — blockers for any production deployment**:
- *State locality*: server restart destroys in-flight work; no multi-replica possible. Fix: Redis swap on three interfaces. Estimated: two to three days.
- *HITL scope*: agents can call bash or write files with no human gate. Fix: general `InterruptRequest` mechanism for any irreversible action.

**P1 — high-priority for user-facing deployment**:
- *No authentication*: any observer of a run ID can read results. Fix: API key middleware. One day.
- *No backpressure*: burst submissions cause unbounded task accumulation. Fix: concurrency semaphore. Two hours.
- *No distributed tracing*: production debugging is very difficult. Fix: OpenTelemetry instrumentation. One to two days.

**P2 — quality and efficiency**:
- *Context window growth*: token costs balloon on deep dependent chains. Fix: typed context slices and message truncation with summarization.
- *Single model for all subtasks*: paying Sonnet prices for formatting work. Fix: complexity-based routing.

North star: stateless orchestrator that survives restart, runs behind a load balancer, with human approval gates on irreversible actions that show reviewers the reasoning trace. Two sprint cycles.

---

## Slide 29 — What's Next

**Sprint 1 (~1 week) — production readiness baseline**:
- Redis-backed RunContext, StreamEmitter, TaskBus → multi-replica deployment and restart safety
- General `InterruptRequest` mechanism → tool-impact gate before any high-impact tool, extended HITL API (approve / reject / modify / restart)

After sprint 1: AgentFlow survives a server restart, runs behind a load balancer, and pauses before irreversible actions. Those two properties define the line between prototype and production system.

**Sprint 2 (~1 week) — security and observability**:
- API key authentication with tenant namespacing
- Concurrency semaphore with HTTP 429
- OpenTelemetry instrumentation across the full call tree
- Prometheus metrics endpoint
- Prompt injection sanitizer on tool outputs

**Sprint 3 (~2 weeks) — efficiency and scale**:
- Typed context slices per agent role
- Message thread truncation with summarization
- Complexity-based model routing
- Postgres warm tier for queryable run history
- Shared result cache across runs
- Celery backend for long-horizon run durability

Four to five weeks of focused engineering from current prototype to production-grade platform.

---

## Slide 30 — Summary

Six things that matter most:

1. **Declarative agents** reduce the cost of experimentation to a YAML file. Domain experts can extend the platform directly.

2. **LLM-powered planning + DAG scheduling** gives natural parallelism without extra effort. The planner discovers what's independent; the scheduler runs it concurrently.

3. **Streaming + prompt caching** makes AI systems feel better and cost significantly less. The effects are independent and compound.

4. **Human-in-the-loop** is a feature, not an afterthought. Approval gates before irreversible actions, with full reasoning trace visible, are what make a system deployable and auditable.

5. **Swappable interfaces from day one** is the difference between a prototype that becomes technical debt and one that becomes production infrastructure.

6. **The foundation is solid.** The gap analysis is a prioritized list of known work with clear time estimates. Two sprint cycles close the critical gaps.

AgentFlow is a composable, streaming, budget-aware multi-agent orchestration platform — built on the right instincts, and two focused sprints from running safely at scale.

---

## Slide 31 — Q&A

Thank you.

Everything is in the repository: source code, `multi-agent-system-design.md` for high-level design intent, and `agentflow_gap_analysis.md` for the detailed production readiness analysis.

Happy to take questions on architecture and design trade-offs, specific implementation choices, deployment scenarios and migration path, gap prioritization, or the research foundations behind the system.

---

*Total estimated speaking time: 35–42 minutes. Allow 5–10 minutes for questions.*
