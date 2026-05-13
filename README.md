# AgentFlow

A multi-agent orchestration system built on FastAPI and Claude. A single task is decomposed into subtasks by an LLM planner, routed to specialised agents across a dependency DAG, and streamed back to the client in real time over SSE.

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
         │ mcp_servers     │   │ mcp_servers     │
         │                 │   │                 │
         │ agentic loop    │   │ agentic loop    │
         │  ↕ tool calls   │   │  ↕ tool calls   │
         └─────────────────┘   └─────────────────┘
```

**Key design decisions**

| Principle | Implementation |
|---|---|
| **Specialisation** | Each agent is fully defined by its JSON manifest — system prompt, tool allow-list, and MCP servers |
| **Isolation** | Agents receive context only via `TaskEnvelope`; they cannot address each other directly |
| **Composability** | The LLM planner routes subtasks to agents at runtime based on the registry summary |
| **Transparency** | Every internal state transition emits a typed SSE event |
| **Extensibility** | New agents register via manifest — no Python code changes required |
| **Resilience** | Per-subtask retry (exponential backoff) + fallback agent support |

---

## Project layout

```
agentflow/
├── src/agentflow/
│   ├── config.py                  # Settings (pydantic-settings, reads .env)
│   ├── main.py                    # FastAPI app — loads registry + engine on startup
│   ├── core/
│   │   ├── models.py              # All message protocol types
│   │   ├── registry.py            # AgentRegistry — manifest loader + capability index
│   │   ├── context.py             # Per-run result store (context propagation)
│   │   └── bus.py                 # Async task bus (asyncio queues; Redis-swappable)
│   ├── orchestrator/
│   │   ├── engine.py              # 7-step pipeline: intake → plan → DAG → dispatch → assemble
│   │   ├── planner.py             # LLM planning pass → structured ExecutionPlan
│   │   ├── scheduler.py           # networkx DAG + topological sort
│   │   └── stream.py              # SSE StreamEmitter / StreamRegistry
│   ├── agents/
│   │   └── agent.py               # Single generic Agent class (manifest-driven)
│   └── tools/
│       ├── registry.py            # ToolDefinition dataclass + ToolRegistry
│       ├── builtin.py             # Built-in tool implementations
│       └── mcp_tools.py           # MCP server connectivity
├── manifests/                     # JSON agent manifests (one file = one agent)
│   ├── research_agent.json
│   ├── code_agent.json
│   ├── data_agent.json
│   ├── writer_agent.json
│   └── planner_agent.json
└── tests/
    ├── test_models.py
    ├── test_registry.py
    ├── test_scheduler.py
    ├── test_tools.py
    └── test_agent.py
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

# Run
uv run uvicorn agentflow.main:app --reload
```

The API is now available at `http://localhost:8000`.

---

## Configuration

All settings are read from environment variables (or a `.env` file).

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(required)* | Anthropic API key |
| `PLANNER_MODEL` | `claude-sonnet-4-6` | Model used for the LLM planning pass |
| `AGENT_MODEL` | `claude-sonnet-4-6` | Model used inside each agent's agentic loop |
| `TASK_TIMEOUT_MS` | `30000` | Per-subtask wall-clock timeout (ms) |
| `TASK_MAX_RETRIES` | `3` | Retry attempts before falling back or failing |
| `TASK_MAX_TOKENS` | `4096` | `max_tokens` passed to each agent call |
| `AGENT_MAX_ITERATIONS` | `10` | Maximum tool-use loop iterations per subtask |
| `MANIFESTS_DIR` | `manifests` | Directory scanned for `*.json` agent manifests |
| `WORKSPACE_DIR` | `workspace` | Sandbox root for file and shell tools |

---

## API

### Start a run

```
POST /api/run
Content-Type: application/json

{
  "task": "Produce a market analysis report on electric vehicles",
  "context": {}
}
```

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

### Health check

```
GET /health
```

```json
{ "status": "ok", "agents": ["ResearchAgent", "CodeAgent", "DataAgent", "WriterAgent", "PlannerAgent"] }
```

### SSE event types

| Type | When emitted |
|---|---|
| `run:started` | Run created, SSE channel open |
| `plan:created` | LLM planner produced the subtask DAG |
| `task:dispatched` | A subtask has been sent to an agent |
| `agent:progress` | Mid-task status or tool call notification |
| `task:complete` | A subtask finished successfully |
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
| **ResearchAgent** | Information Retrieval | `web_search`, `fetch_url`, `wikipedia`, `arxiv_search`, `file_write` |
| **CodeAgent** | Software Engineering | `bash_exec`, `python_exec`, `file_read`, `file_write`, `lint`, `test_runner` |
| **DataAgent** | Data Analysis | `python_exec`, `file_read`, `file_write`, `sql_query`, `csv_parse`, `chart_gen` |
| **WriterAgent** | Content & Communication | `web_search`, `fetch_url`, `file_read`, `file_write`, `spell_check`, `readability_score`, `tone_analyzer` |
| **PlannerAgent** | Strategy & Decomposition | `web_search`, `file_read`, `file_write`, `dependency_graph`, `timeline_gen`, `risk_model` |

### Manifest schema

```json
{
  "agent_id": "ResearchAgent",
  "version": "1.0.0",
  "domain": "Information Retrieval",
  "capabilities": ["web_research", "document_synthesis", "citation"],
  "tools": ["web_search", "fetch_url", "wikipedia"],
  "mcp_servers": [
    { "name": "brave-search", "url": "http://localhost:3001/sse" }
  ],
  "system_prompt": "You are an expert research analyst...",
  "fallback_for": [],
  "max_concurrency": 3
}
```

| Field | Description |
|---|---|
| `agent_id` | Unique identifier referenced by the planner and other agents |
| `capabilities` | Human-readable skill tags — included in the planner prompt |
| `tools` | Allow-list of built-in tool names this agent may call |
| `mcp_servers` | Remote MCP servers; all their tools are added automatically |
| `system_prompt` | Full agent persona and output format instructions |
| `fallback_for` | `agent_id`s this agent can substitute for on failure |
| `max_concurrency` | Maximum parallel instances (informational — enforced by scheduler) |

### How agents work

Every agent is an instance of the single `Agent` class in `agents/agent.py`. When dispatched a `TaskEnvelope`:

1. Built-in tools listed in `tools` are loaded from the global registry
2. Each MCP server in `mcp_servers` is connected; its tools are discovered and merged in
3. The agentic loop runs — Claude is called with the merged tool list, executes any tool calls in parallel, feeds results back, and repeats until `end_turn` or the iteration limit
4. The final text response is returned as the subtask result (JSON-parsed where possible for structured downstream consumption)

---

## Tools

### Built-in tools

| Tool | Status | Description |
|---|---|---|
| `fetch_url` | ✅ | HTTP GET a URL; returns up to 8 000 characters |
| `web_search` | ✅ | DuckDuckGo instant answers — no API key required |
| `wikipedia` | ✅ | Wikipedia REST summary API |
| `file_read` | ✅ | Read a file from the workspace directory |
| `file_write` | ✅ | Write a file to the workspace directory |
| `bash_exec` | ✅ | Run a shell command in the workspace; configurable timeout |
| `python_exec` | ✅ | Execute a Python snippet; configurable timeout |
| `arxiv_search` | 🔧 stub | Requires arXiv API integration or MCP server |
| `sql_query` | 🔧 stub | Requires database connection configuration |
| `csv_parse` | 🔧 stub | Requires MCP server or custom implementation |
| `chart_gen` | 🔧 stub | Requires rendering backend |
| `spell_check` | 🔧 stub | Requires grammar API or MCP server |
| `readability_score` | 🔧 stub | Requires text analysis library |
| `tone_analyzer` | 🔧 stub | Requires sentiment API or MCP server |
| `dependency_graph` | 🔧 stub | Requires graph rendering backend |
| `timeline_gen` | 🔧 stub | Requires scheduling library |
| `risk_model` | 🔧 stub | Requires custom implementation |
| `lint` | 🔧 stub | Wrap `bash_exec` with a linter command |
| `test_runner` | 🔧 stub | Wrap `bash_exec` with a test command |

Stubs return an informative message so the LLM can gracefully work around missing capabilities rather than crashing.

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
| `python-dotenv` | `.env` file loading |

### Implementation phases

| Phase | Status | Scope |
|---|---|---|
| 1 — Core infrastructure | ✅ done | Models, registry, bus, SSE emitter, isolated agent context |
| 2 — Orchestrator engine | ✅ done | LLM planner, DAG scheduler, context propagation, retry + fallback |
| 3 — Agent + tool layer | ✅ done | Generic manifest-driven agent, built-in tools, MCP connectivity |
| 4 — Streaming & client UX | 🔲 next | Partial result streaming, client-side event parser |
| 5 — Observability & hardening | 🔲 planned | OpenTelemetry traces, token cost tracking, load testing |
