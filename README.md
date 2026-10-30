# AgentFlow

A multi-agent orchestration system built on FastAPI and Claude. A single task is decomposed into subtasks by an LLM planner, routed to specialised agents across a dependency DAG, and streamed back to the client in real time over SSE.

> **Understanding the system:** start with the **[architecture guide](docs/guide/README.md)**: overview, concepts, flows with diagrams, storage, deployment modes, and [what's new](docs/guide/whats-new.md). The section below is a quick reference.

---

## Architecture

```
Client
  │  POST /api/run  { task, context }
  │  GET  /api/run/:id/stream  ← SSE
  ▼
┌─────────────────────────────────────────────────────┐
│                   Orchestrator                       │
│                                                      │
│  1. LLM Planner  →  ExecutionPlan (subtask DAG)     │
│  2. Scheduler    →  topological order + parallelism  │
│  3. Engine       →  dispatch, retry, fallback        │
│  4. StreamEmitter→  typed SSE events per transition  │
└────────────────────────┬────────────────────────────┘
                         │  TaskEnvelope (per subtask)
              ┌──────────┴──────────┐
              ▼                     ▼
         Agent (manifest A)    Agent (manifest B)
         ┌─────────────────┐   ┌─────────────────┐
         │ system_prompt   │   │ system_prompt   │
         │ tool allow-list │   │ tool allow-list │
         │ skills          │   │ skills          │
         │ mcp_servers     │   │ mcp_servers     │
         │                 │   │                 │
         │ agentic loop    │   │ agentic loop    │
         │  ↕ tool calls   │   │  ↕ tool calls   │
         └─────────────────┘   └─────────────────┘
```

**Key design decisions**

| Principle | Implementation |
|---|---|
| **Specialisation** | Each agent is fully defined by its JSON manifest — system prompt, tool allow-list, skills, and MCP servers |
| **Isolation** | Agents receive context only via `TaskEnvelope`; they cannot address each other directly |
| **Composability** | The LLM planner routes subtasks to agents at runtime based on the registry summary |
| **Transparency** | Every internal state transition emits a typed SSE event |
| **Extensibility** | New agents register via manifest — no Python code changes required |
| **Resilience** | Per-subtask retry (exponential backoff) + fallback agent support |
| **Budget-awareness** | Agents compute `max_tokens` from the remaining USD budget and auto-stop when the minimum iteration budget is exhausted |
| **Prompt caching** | Anthropic cache headers are injected automatically on every call to reduce cost |

---

## Project layout

```
agentflow/
├── src/agentflow/
│   ├── config.py                  # Settings (pydantic-settings, reads .env)
│   ├── main.py                    # FastAPI app — loads registry + engine on startup
│   ├── logging_config.py          # Structured logging setup
│   ├── core/
│   │   ├── models.py              # All message protocol types
│   │   ├── registry.py            # AgentRegistry — manifest loader + capability index
│   │   ├── context.py             # Per-run result store, budget tracking (context propagation)
│   │   ├── bus.py                 # Async task bus (asyncio queues; Redis-swappable)
│   │   └── skill_loader.py        # Loads SKILL.md headers and reference docs from skills/
│   ├── orchestrator/
│   │   ├── engine.py              # 7-step pipeline: intake → plan → DAG → dispatch → assemble
│   │   ├── planner.py             # LLM planning pass with workspace exploration (ReAct loop)
│   │   ├── decomposer.py          # Expands code-related subtasks into micro-subtasks
│   │   ├── scheduler.py           # networkx DAG + topological sort
│   │   ├── reporter.py            # Compiles the final report from agent outputs
│   │   └── stream.py              # SSE StreamEmitter / StreamRegistry
│   ├── agents/
│   │   └── agent.py               # Single generic Agent class (manifest-driven)
│   ├── tools/
│   │   ├── registry.py            # ToolDefinition dataclass + ToolRegistry (with ToolImpact)
│   │   ├── builtin.py             # Built-in tool implementations
│   │   ├── skills.py              # read_skill tool for accessing skill documentation
│   │   └── mcp_tools.py           # MCP server connectivity
│   ├── llm/
│   │   └── client.py              # LLMClient — Anthropic SDK wrapper with prompt caching
│   ├── api/
│   │   └── routes.py              # FastAPI route definitions
│   └── cli/
│       ├── __init__.py            # Click CLI: run / serve / health subcommands
│       ├── client.py              # HTTP client for API calls
│       └── display.py             # Rich console output for streaming events
├── manifests/                     # JSON agent manifests (one file = one agent)
│   ├── research_agent.json
│   ├── code_agent.json
│   ├── data_agent.json
│   ├── writer_agent.json
│   ├── planner_agent.json
│   ├── financial_analyst_agent.json
│   └── frontend_agent.json
├── skills/                        # Domain skill packs (SKILL.md + reference docs)
├── tests/
│   ├── test_models.py
│   ├── test_registry.py
│   ├── test_scheduler.py
│   ├── test_tools.py
│   └── test_agent.py
└── tests-smoke/
    └── tests.http                 # REST client requests for manual integration testing
```

---

## Quickstart

**Prerequisites:** Python 3.13+, [`uv`](https://docs.astral.sh/uv/)

```bash
git clone <repo>
cd agentflow

# Install dependencies
uv sync

# Configure
cp .env.example .env
# Edit .env — at minimum set ANTHROPIC_API_KEY

# Run the server
uv run uvicorn agentflow.main:app --reload

# Or use the CLI directly
uv run agentflow run "Produce a market analysis report on electric vehicles"
```

The API is available at `http://localhost:8000`.

---

## CLI

```bash
# Submit a task and stream results to the terminal
agentflow run "<task>" [--context KEY=VALUE ...] [--verbose] [--json]

# Start the FastAPI server
agentflow serve [--reload]

# Check server health and registered agents
agentflow health
```

---

## Configuration

All settings are read from environment variables (or a `.env` file).

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(required)* | Anthropic API key |
| `PLANNER_MODEL` | `claude-sonnet-5` | Model used for the LLM planning pass |
| `AGENT_MODEL` | `claude-sonnet-5` | Model used inside each agent's agentic loop |
| `REPORTER_MODEL` | `claude-haiku-4-5-20251001` | Model used to compile the final report |
| `TASK_TIMEOUT_MS` | `3600000` | Per-subtask wall-clock timeout (ms) |
| `TASK_MAX_RETRIES` | `1` | Retry attempts before falling back or failing |
| `AGENT_MAX_TOKENS_FALLBACK` | `8192` | `max_tokens` when no budget is set for the subtask |
| `AGENT_MAX_ITERATIONS` | `10` | Maximum tool-use loop iterations per subtask |
| `AGENT_MIN_ITERATION_BUDGET_USD` | `0.002` | Minimum remaining USD budget to attempt another iteration |
| `AGENT_THINKING_EFFORT` | `high` | Global adaptive-thinking effort (`low`/`medium`/`high`/`xhigh`/`max`); `""` disables. Manifest `thinking_effort` overrides per agent. |
| `PLANNER_MAX_ITERATIONS` | `15` | Maximum tool-use iterations for the planning pass |
| `ENABLE_PROMPT_CACHING` | `true` | Inject Anthropic cache headers on every call |
| `CAPTURE_EVENTS` | `false` | Write SSE events to `.runs/{id}/events.jsonl` |
| `CAPTURE_RESULTS` | `false` | Write subtask results to `.runs/{id}/results.jsonl` |
| `DISPATCH_MODE` | `local` | `workers` sends subtask agents to `agentflow worker` processes through Redis, while planning, scheduling, verification, checkpoints and the session log stay on the server. Needs `STATE_BACKEND=redis`, and a `.runs` directory and workspace shared with the workers. |
| `WORKER_CLAIM_TIMEOUT_S` / `WORKER_HEARTBEAT_S` | `60` / `5` | A queued task that no worker picks up within the timeout fails and is retried or handed to a fallback agent. A worker that misses 3 heartbeats is presumed dead, with the same outcome. |
| `SANDBOX_BACKEND` | `local` | Where `bash_exec`, `bash_exec_readonly` and `python_exec` run. `local` is a host subprocess with a scrubbed environment. `docker` is one container per run, started on the first command, with the workspace mounted at `/workspace`, all capabilities dropped and no host credentials. With `docker`, the run fails rather than running on the host if Docker is unavailable. |
| `SANDBOX_DOCKER_IMAGE` / `_NETWORK` / `_MEMORY` / `_CPUS` / `_PIDS_LIMIT` | `python:3.13-slim` / `bridge` / `2g` / `2` / `512` | Container settings. Set the network to `none` to cut the sandbox off from the network (package installs will then fail). |
| `ENABLE_VERIFIER` | `true` | Subtasks the planner gave `acceptanceCriteria` are checked by an independent read-only verifier before counting as done. Unmet criteria go back to the agent to fix. |
| `VERIFICATION_MAX_REPAIRS` | `1` | Fix-and-recheck rounds before a still-failing subtask is downgraded to partial |
| `VERIFIER_MODEL_TIER` / `VERIFIER_MAX_ITERATIONS` | `economy` / `8` | Verifier cost controls |
| `ENABLE_CHECKPOINTS` | `true` | Saves git checkpoints of each run's workspace in a separate repository at `.runs/{id}/checkpoints.git`: a baseline, then one per finished subtask. Before a retry, files written by the failed attempt are rolled back. A workspace's own `.git` is never touched. Needs `git`. |
| `CAMPAIGN_MAX_FEATURES` | `30` | Maximum number of features in a campaign's feature list |
| `CAMPAIGN_MAX_FEATURE_ATTEMPTS` | `2` | Sessions a feature may fail verification before it is marked `blocked` |
| `CAMPAIGN_REGRESSION_CHECKS` | `1` | Features that already pass and are re-verified before each session |
| `CONTEXT_SNIP_TRIGGER_TOKENS` | `100000` | Once an agent's prompt passes this size, older tool results are replaced in what the model sees by stubs it can expand with `recall_tool_result`. The full history is kept. `0` disables this. |
| `CONTEXT_SNIP_KEEP_RECENT` | `3` | Most recent tool-result turns always shown in full |
| `CONTEXT_SNIP_MIN_CHARS` | `1000` | Tool results shorter than this are never replaced by a stub |
| `RESUME_INTERRUPTED_RUNS` | `false` | Resume runs interrupted by a restart automatically at startup (spends budget without anyone asking) |
| `RUN_LEASE_TTL_S` | `15` | Redis backend: TTL of the per-run ownership lease. The owner renews it every TTL/3 seconds. A crashed replica's runs become resumable once the lease lapses, and a cancel sent to another replica takes effect within about TTL/3 seconds. |
| `MANIFESTS_DIR` | `manifests` | Directory scanned for `*.json` agent manifests |
| `WORKSPACE_DIR` | `workspace` | Root for per-run workspaces: each run's file and shell tools are confined to `workspace/runs/{id}/` or a named `workspace/{name}/` |
| `SKILLS_DIR` | `skills` | Directory scanned for skill packs |

---

## API

### Start a run

```
POST /api/run
Content-Type: application/json

{
  "task": "Produce a market analysis report on electric vehicles",
  "context": {},
  "workspace": null
}
```

`workspace` (optional) names a long-lived project workspace, `workspace/{name}/`
(letters, digits, `.`, `_`, `-`; `runs` is reserved), for runs that keep iterating
on one project. Runs that share a named workspace execute one at a time. When it
is omitted, the run gets a fresh, isolated `workspace/runs/{run_id}/`. A followup
(`POST /api/runs/{id}/followup`) continues in the prior run's workspace unless it
names another one. CLI: `agentflow run --workspace NAME "<task>"`.

**Response**

```json
{ "run_id": "550e8400-e29b-41d4-a716-446655440000", "status": "started" }
```

### Stream events

```
GET /api/run/{run_id}/stream
Accept: text/event-stream
```

Returns a stream of Server-Sent Events. Each event's `data` field is a JSON object:

```json
{
  "run_id": "550e8400-...",
  "seq": 7,
  "ts": 1715123456789,
  "type": "agent:progress",
  "agent_id": "ResearchAgent",
  "payload": {
    "message": "Calling tool: web_search",
    "data": { "tool": "web_search", "input": { "query": "EV market 2024" } }
  }
}
```

### Session log

```
GET /api/runs/{run_id}/session?after_seq=0&type=message_appended&invocation_id=<id>
```

Every run keeps an always-on, append-only log at `.runs/{run_id}/session.jsonl`. It
holds the run's inputs, the plan, subtask dispatches and results, cost, human-input
exchanges, and the full message history of every agent invocation. The SSE stream is
a live view for clients, while the session log is the record the run can be rebuilt
from. All query parameters are optional, and `type` can be repeated. The endpoint
works while the run is still in progress.

### Worker processes

```bash
# server: DISPATCH_MODE=workers STATE_BACKEND=redis uv run uvicorn agentflow.main:app
uv run agentflow worker                              # serve every agent in manifests/
uv run agentflow worker --agents CodeAgent --concurrency 4
```

Workers take subtask agent invocations from per-agent Redis queues. Each runs the
agent with its own sandbox, streams progress to the run's SSE stream, and sends the
transcript and result back to the server. The server stays the only writer of the
session log. A worker that dies mid-task, or a task no worker claims, becomes a failed
attempt, so the normal retry and fallback logic takes over. Cancelling a run stops its
remote agents.

### Resume an interrupted run

```
POST /api/runs/{run_id}/resume
```

If the service stops while a run is in progress, the next startup marks the run
interrupted instead of ending it. Resuming continues the same `run_id`, and you can
reattach to the same `/stream` URL. Subtasks that had finished are not run again,
the logged plan is reused, and a subtask that was mid-flight continues from its
last logged agent turn. Any tool request that was still executing is dropped, and
the model issues it again if it still needs it.

Returns `409` if the run is still running (on this replica, or on another replica
that still holds its lease), has no session log (it predates session logs), or
already finished. Set `RESUME_INTERRUPTED_RUNS=true` to resume automatically at
startup. With `STATE_BACKEND=redis`, `POST /api/runs/{id}/cancel` also reaches runs
that execute on another replica and returns `{"status": "cancel_requested"}`.

### Campaigns (long-running work across sessions)

```
POST /api/campaigns          {"task": "<spec>", "agent_id": "CodeAgent", "max_sessions": 20,
                              "budget_usd": 25, "session_budget_usd": 3, "workspace": "my-app"}
GET  /api/campaigns          GET /api/campaigns/{id}
POST /api/campaigns/{id}/pause   POST /api/campaigns/{id}/resume
```

A campaign builds something too large for one run, in sessions that each start with
a fresh context:

1. **Feature list.** A read-only planner turns the spec into an ordered list of
   features. Each feature has checks that can be confirmed by reading files. The list
   is stored in `.runs/campaigns/{id}/features.json`, outside the agents' workspace,
   and its feature definitions never change after that.
2. **Sessions.** Each session is a normal run (with its own stream, session log,
   checkpoints and resume) that implements one feature. Its acceptance criteria are
   that feature's checks. **A feature is marked passing only when the independent
   verifier confirms it**, never because the agent says so.
3. **Regression checks.** Before each session, a feature that already passes is
   re-checked. If it no longer holds, it is fixed next.
4. **Stopping.** A campaign stops when every feature passes, when it reaches
   `max_sessions` or `budget_usd`, or when only `blocked` features are left.

`pause` takes effect after the current session finishes. To stop sooner, cancel that
session's run. A service restart pauses running campaigns. `resume` first finishes
the interrupted session and then continues. They resume automatically if
`RESUME_INTERRUPTED_RUNS=true`.

### Workspace checkpoints

```
GET /api/runs/{run_id}/checkpoints
```

Lists the run's workspace snapshots, oldest first: `sha`, `label`, `subtask_id`, and
the files `changed` in each one. To restore the workspace to one by hand:

```bash
git --git-dir .runs/<run_id>/checkpoints.git --work-tree <workspace> checkout <sha> -- .
```

### Health check

```
GET /health
```

```json
{ "status": "ok", "agents": ["ResearchAgent", "CodeAgent", "DataAgent", "WriterAgent", "PlannerAgent", "FinancialAnalystAgent", "FrontendAgent"] }
```

### SSE event types

| Type | When emitted |
|---|---|
| `run:started` | Run created, SSE channel open |
| `plan:created` | LLM planner produced the subtask DAG |
| `task:dispatched` | A subtask has been sent to an agent |
| `agent:progress` | Mid-task status or tool call notification |
| `agent:query` | Agent needs information from another agent (back-channel) |
| `task:complete` | A subtask finished successfully |
| `task:verified` | An independent verifier checked a subtask's acceptance criteria (`data.verification`: passed / failed / inconclusive per criterion) |
| `task:failed` | A subtask failed after all retries |
| `run:complete` | All subtasks done; assembled result attached |
| `run:error` | Unrecoverable orchestrator-level error |

### Client example (JavaScript)

```javascript
const { run_id } = await fetch('/api/run', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ task: 'Analyse EV market trends 2020–2025' }),
}).then(r => r.json());

const source = new EventSource(`/api/run/${run_id}/stream`);

source.addEventListener('message', (e) => {
  const event = JSON.parse(e.data);
  switch (event.type) {
    case 'plan:created':   console.log('Plan:', event.payload.data); break;
    case 'agent:progress': console.log(event.agent_id, event.payload.message); break;
    case 'run:complete':   console.log('Done:', event.payload.data); source.close(); break;
    case 'run:error':      console.error(event.payload.message); source.close(); break;
  }
});
```

---

## Agents

Each agent is defined by a JSON manifest in `manifests/`. No Python code changes are needed to add a new agent — drop in a manifest file and restart.

### Built-in agents

| Agent | Domain | Tools |
|---|---|---|
| **ResearchAgent** | Information Retrieval | `web_search`, `fetch_url`, `file_read`, `file_write` |
| **CodeAgent** | Software Engineering | `bash_exec`, `python_exec`, `file_read`, `file_write` |
| **DataAgent** | Data Analysis | `python_exec`, `bash_exec`, `file_read`, `file_write` |
| **WriterAgent** | Content & Communication | `file_read`, `file_write` |
| **PlannerAgent** | Strategy & Decomposition | `web_search`, `file_read`, `file_write` |
| **FinancialAnalystAgent** | Financial Analysis | `web_search`, `fetch_url`, `python_exec`, `file_read`, `file_write` |
| **FrontendAgent** | Frontend Development | `bash_exec`, `python_exec`, `file_read`, `file_write` |

### Manifest schema

```json
{
  "agent_id": "ResearchAgent",
  "version": "1.0.0",
  "domain": "Information Retrieval",
  "capabilities": ["web_research", "document_synthesis", "citation"],
  "tools": ["web_search", "fetch_url", "file_read", "file_write"],
  "skills": ["technical-analysis"],
  "mcp_servers": [
    { "name": "brave-search", "url": "http://localhost:3001/sse" }
  ],
  "system_prompt": "You are an expert research analyst...",
  "fallback_for": [],
  "max_concurrency": 3,
  "max_iterations": 8
}
```

| Field | Description |
|---|---|
| `agent_id` | Unique identifier referenced by the planner and other agents |
| `capabilities` | Human-readable skill tags — included in the planner prompt |
| `tools` | Allow-list of built-in tool names this agent may call |
| `skills` | Skill pack names from `skills/`; injects `read_skill` into the agent's tool list |
| `mcp_servers` | Remote MCP servers; all their tools are added automatically |
| `system_prompt` | Full agent persona and output format instructions |
| `fallback_for` | `agent_id`s this agent can substitute for on failure |
| `max_concurrency` | Maximum parallel instances (enforced by scheduler) |
| `max_iterations` | Per-agent override for the tool-use iteration limit |
| `tool_limits` | Per-task call budgets, e.g. `{"fetch_url": 5}` |
| `tool_policies` | Checks run before every tool call. `{"tool": "bash_exec", "allow_commands": ["ls", "git", "npm", "pytest"]}` means every command in a `bash_exec` pipeline must be on the list. Command substitution, subshells and multi-line commands are refused. Avoid listing command runners such as `sh`, `env` or `xargs`, which would allow anything. |

### How agents work

Every agent is an instance of the single `Agent` class in `agents/agent.py`. When dispatched a `TaskEnvelope`:

1. Built-in tools listed in `tools` are loaded from the global registry
2. If `skills` are declared, the `read_skill` tool is injected so the agent can pull documentation on demand
3. Each MCP server in `mcp_servers` is connected; its tools are discovered and merged in
4. The agentic loop runs — Claude is called with the merged tool list, executes any tool calls in parallel, feeds results back, and repeats until `end_turn` or the iteration limit
5. `max_tokens` is computed from the remaining USD budget for the run; the loop stops early if the budget falls below `AGENT_MIN_ITERATION_BUDGET_USD`
6. The final text response is returned as the subtask result (JSON-parsed where possible for structured downstream consumption)

---

## Tools

### Built-in tools

| Tool | Impact | Description |
|---|---|---|
| `fetch_url(url)` | read | HTTP GET a URL; returns up to 8 000 characters |
| `web_search(query, max_results?)` | read | DuckDuckGo instant answers — no API key required |
| `file_read(path, start_line?, num_lines?)` | read | Read a workspace file; up to 200 lines per call (use `start_line` for incremental reading) |
| `file_write(path, content)` | write | Write or append a file in the workspace directory |
| `bash_exec(command, purpose)` | execute | Run a shell command in the workspace; `purpose` is required |
| `python_exec(code, purpose)` | execute | Execute a Python snippet in a sandbox `.venv`; `purpose` is required |
| `read_skill(skill, topics?)` | read | Load SKILL.md and selected reference docs from the skills directory |

> **Note on execute-type tools:** `bash_exec` and `python_exec` require a `purpose` field describing the intent of the command. This is included in progress events for observability.

### Adding a built-in tool

Register a `ToolDefinition` in `src/agentflow/tools/builtin.py`:

```python
async def _my_tool(query: str) -> str:
    # ... implementation ...
    return result_string

tool_registry.register(ToolDefinition(
    name="my_tool",
    description="What this tool does and when to use it.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The input"}
        },
        "required": ["query"],
    },
    handler=_my_tool,
    impact=ToolImpact.read_only,
))
```

Then add `"my_tool"` to the `tools` array of any manifest that should have access to it.

### Connecting an MCP server

Add an entry to the `mcp_servers` array of a manifest. The server must expose an SSE endpoint:

```json
"mcp_servers": [
  { "name": "brave", "url": "http://localhost:3001/sse" }
]
```

All tools exposed by that server are automatically discovered at agent run time and made available to Claude alongside the built-ins. If the server is unreachable the agent logs a warning and continues with only its built-in tools.

---

## Skills

Skills are domain-specific knowledge packs that agents can load at runtime. Each skill lives in its own subdirectory under `skills/` and consists of a `SKILL.md` index file plus one or more reference documents.

```
skills/
├── python-coding/
│   ├── SKILL.md
│   ├── best_practices.md
│   └── uv_guide.md
├── technical-analysis/
├── frontend-web/
├── financial-analysis/
├── equity-research/
└── ...
```

**SKILL.md format:**

```markdown
---
name: python-coding
description: Python best practices, tooling, and style guidelines
---

# Python Coding

Overview and usage guidelines.

## Reference Documents

- `best_practices.md` — PEP 8, typing, testing conventions
- `uv_guide.md` — Package and environment management with uv
```

When an agent's manifest declares `"skills": ["python-coding"]`, the `read_skill` tool is automatically added to its tool list. The agent can then call:

```
read_skill(skill="python-coding", topics=["best_practices.md"])
```

This returns the skill overview plus the requested reference documents, injecting up-to-date guidance into the agent's context without bloating the system prompt.

---

## Message protocols

### Task Envelope (Orchestrator → Agent)

```json
{
  "task_id": "uuid-v4",
  "parent_run_id": "uuid-v4",
  "agent_id": "ResearchAgent",
  "instruction": "Gather EV market data for 2020–2025",
  "context": {
    "prior_results": {},
    "shared_memory": {}
  },
  "constraints": {
    "max_tokens": 4096,
    "timeout_ms": 30000
  }
}
```

### Agent Result (Agent → Orchestrator)

```json
{
  "task_id": "uuid-v4",
  "agent_id": "ResearchAgent",
  "status": "success",
  "output": {
    "structured": {},
    "text": "Summary of findings..."
  },
  "tokens_used": 1842,
  "duration_ms": 4200
}
```

---

## Development

### Run tests

```bash
uv run pytest tests/ -v
```

### Dependencies

| Package | Purpose |
|---|---|
| `anthropic` | Claude API client |
| `fastapi` + `uvicorn` | HTTP server and ASGI runner |
| `sse-starlette` | Server-Sent Events for FastAPI |
| `pydantic` + `pydantic-settings` | Data validation and config |
| `networkx` | DAG construction and topological sort |
| `mcp` | MCP client for remote tool servers |
| `httpx` | HTTP client used by built-in tools |
| `click` | CLI framework |
| `rich` | Terminal output for CLI streaming display |
| `python-dotenv` | `.env` file loading |

### Implementation phases

| Phase | Status | Scope |
|---|---|---|
| 1 — Core infrastructure | ✅ done | Models, registry, bus, SSE emitter, isolated agent context |
| 2 — Orchestrator engine | ✅ done | LLM planner, DAG scheduler, context propagation, retry + fallback |
| 3 — Agent + tool layer | ✅ done | Generic manifest-driven agent, built-in tools, MCP connectivity, skills system |
| 4 — CLI & budget-aware execution | ✅ done | Click CLI, Rich streaming display, USD budget tracking, prompt caching |
| 5 — Observability & hardening | 🔲 planned | OpenTelemetry traces, token cost tracking, load testing |
