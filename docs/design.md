# AgentFlow — Design Document

> **Single source of truth for architecture, data contracts, and implementation decisions.**
> Changes to this document describe the intended behaviour; a coding agent given this document should be able to re-implement the system from scratch.

---

## Table of Contents

1. [Project Purpose and Goals](#1-project-purpose-and-goals)
2. [Design Principles](#2-design-principles)
3. [Repository Layout](#3-repository-layout)
4. [Architecture Overview](#4-architecture-overview)
5. [Configuration](#5-configuration)
6. [Data Models](#6-data-models)
7. [API Endpoints](#7-api-endpoints)
8. [Agent System](#8-agent-system)
9. [Tool System](#9-tool-system)
10. [Orchestration Engine](#10-orchestration-engine)
11. [Planning System](#11-planning-system)
12. [Scheduling](#12-scheduling)
13. [SSE Streaming](#13-sse-streaming)
14. [LLM Client](#14-llm-client)
15. [Context and Budget System](#15-context-and-budget-system)
16. [Skill System](#16-skill-system)
17. [Task Bus](#17-task-bus)
18. [Reporter](#18-reporter)
19. [CLI](#19-cli)
20. [Dependencies](#20-dependencies)
21. [Key Design Decisions](#21-key-design-decisions)
22. [Known Issues and Future Work](#22-known-issues-and-future-work)

---

## 1. Project Purpose and Goals

AgentFlow is a **multi-agent orchestration system** that decomposes a single user task into subtasks, routes those subtasks across a dependency DAG of specialised agents, and streams all internal events back to the client in real time.

The system is built on:
- **FastAPI** — HTTP API and SSE streaming server
- **Anthropic Claude** — LLM for planning, agent execution, and report synthesis
- **asyncio** — concurrent subtask dispatch within a single Python process

---

## 2. Design Principles

| Principle | Implementation |
|---|---|
| **Specialisation** | Each agent is fully defined by a JSON manifest — system prompt, allowed tools, skills, MCP servers. |
| **Isolation** | Agents receive context only via `TaskEnvelope`. They cannot address each other directly. |
| **Composability** | The LLM planner assigns subtasks to agents at runtime from a registry summary. |
| **Transparency** | Every internal state transition emits a typed SSE event. |
| **Extensibility** | A new agent requires only a manifest file — no Python changes. |
| **Resilience** | Per-subtask retry with exponential backoff plus a declared fallback agent mechanism. |
| **Budget-awareness** | Agents compute `max_tokens` from remaining USD budget and auto-stop below a minimum threshold. |
| **Prompt caching** | Anthropic cache headers are injected automatically on every LLM call. |

---

## 3. Repository Layout

```
agentflow/
├── pyproject.toml                     # Package metadata, deps, entry points, pytest config
├── README.md
├── .env.example                       # Template .env file
├── docs/
│   └── design.md                      # This document
├── manifests/                         # JSON agent manifests (one file per agent)
│   ├── research_agent.json
│   ├── code_agent.json
│   ├── data_agent.json
│   ├── writer_agent.json
│   ├── planner_agent.json
│   ├── financial_analyst_agent.json
│   └── frontend_agent.json
├── skills/                            # Skill packs: each subdirectory has SKILL.md + reference docs
├── workspace/                         # Runtime sandbox (auto-created); gitignored
│   └── runs/
│       └── {run_id}/
│           ├── events.jsonl           # Written when capture_events=true
│           ├── results.jsonl          # Written when capture_results=true
│           └── report.md             # Final synthesis report
├── src/agentflow/
│   ├── config.py
│   ├── main.py
│   ├── logging_config.py
│   ├── core/
│   │   ├── models.py
│   │   ├── registry.py
│   │   ├── context.py
│   │   ├── bus.py
│   │   └── skill_loader.py
│   ├── orchestrator/
│   │   ├── engine.py
│   │   ├── planner.py
│   │   ├── decomposer.py
│   │   ├── scheduler.py
│   │   ├── reporter.py
│   │   └── stream.py
│   ├── agents/
│   │   └── agent.py
│   ├── tools/
│   │   ├── registry.py
│   │   ├── builtin.py
│   │   ├── skills.py
│   │   └── mcp_tools.py
│   ├── llm/
│   │   └── client.py
│   ├── api/
│   │   └── routes.py
│   └── cli/
│       ├── __init__.py
│       ├── client.py
│       └── display.py
├── tests/
│   ├── test_models.py
│   ├── test_registry.py
│   ├── test_scheduler.py
│   ├── test_tools.py
│   └── test_agent.py
└── tests-smoke/
    └── tests.http
```

---

## 4. Architecture Overview

```
┌────────────────────────────────────────────────────────────────────┐
│  Presentation                                                      │
│  ┌──────────────────┐   ┌──────────────────────────────────────┐  │
│  │  FastAPI Routes  │   │  Click CLI + Rich display            │  │
│  │  POST /api/run   │   │  agentflow run <task>                │  │
│  │  GET  /api/run/  │   │  agentflow serve                     │  │
│  │       {id}/stream│   │  agentflow health                    │  │
│  └────────┬─────────┘   └─────────────────────────────────────┘  │
└───────────┼────────────────────────────────────────────────────────┘
            │ BackgroundTask
┌───────────▼────────────────────────────────────────────────────────┐
│  Orchestration                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  OrchestratorEngine.run()                                    │  │
│  │  1. Create RunContext + StreamEmitter + TaskBus              │  │
│  │  2. LLM Planner → ExecutionPlan (DAG of Subtasks)           │  │
│  │  3. Decomposer expands coding subtasks into micro-subtasks  │  │
│  │  4. DependencyGraph (networkx) + scheduling loop            │  │
│  │  5. Dispatch subtasks → asyncio tasks → Agent.run()         │  │
│  │  6. Retry / fallback / continuation on partial results      │  │
│  │  7. Reporter synthesises leaf results → report.md           │  │
│  └──────────────────────────────────────────────────────────────┘  │
└───────────┬────────────────────────────────────────────────────────┘
            │ agent.run(TaskEnvelope)
┌───────────▼────────────────────────────────────────────────────────┐
│  Agent Layer                                                       │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  Agent._agentic_loop()  (ReAct: LLM → tool call → result)   │  │
│  │  Budget-aware max_tokens · Continuation on partial           │  │
│  └──────────────────────────────────────────────────────────────┘  │
└───────────┬────────────────────────────────────────────────────────┘
            │
┌───────────▼──────────────┐   ┌──────────────────────────────┐
│  Tool Layer               │   │  LLM Layer                   │
│  Built-ins (12 real,      │   │  LLMClient                   │
│  12 stubs)                │   │  Rate limiting               │
│  MCP tools (SSE client)   │   │  Prompt cache injection      │
│  Skills (read_skill)      │   │  Usage tracking              │
└──────────────────────────┘   └──────────────────────────────┘
```

**Data flow for a single run:**

```
POST /api/run
  → BackgroundTask: engine.run(run_id, task, context, budget_usd)
  → StreamEmitter created (asyncio.Queue)
  → POST returns RunResponse immediately

GET /api/run/{run_id}/stream
  → EventSourceResponse reads from StreamEmitter queue
  → SSE events streamed to client as they are emitted

engine.run pipeline:
  emit run:started
  → planner.create_plan()  →  ExecutionPlan
  → decomposer.expand_plan()
  emit plan:created
  → scheduling loop:
      for each ready subtask (deps satisfied):
        asyncio.create_task(_dispatch_subtask)
        emit task:dispatched
        agent.run() → AgentResult
        emit task:complete | task:partial | task:failed
  → reporter.compile_report()
  emit run:complete | run:error
  → StreamEmitter.close()  (sentinel None on queue)
```

---

## 5. Configuration

All settings live in `src/agentflow/config.py`, implemented as a `pydantic-settings` `BaseSettings` class that reads from environment variables and an optional `.env` file.

### Settings Reference

| Field | Env Var | Default | Description |
|---|---|---|---|
| `anthropic_api_key` | `ANTHROPIC_API_KEY` | `""` | Anthropic API key — **required** |
| `planner_model` | `PLANNER_MODEL` | `claude-sonnet-4-6` | Model used by the planner and decomposer |
| `agent_model` | `AGENT_MODEL` | `claude-sonnet-4-6` | Model used by agent agentic loops |
| `reporter_model` | `REPORTER_MODEL` | `claude-haiku-4-5-20251001` | Model used for final report synthesis |
| `task_timeout_ms` | `TASK_TIMEOUT_MS` | `3_600_000` | Per-subtask wall-clock timeout (ms) |
| `task_max_retries` | `TASK_MAX_RETRIES` | `1` | Retry attempts before fallback/fail |
| `manifests_dir` | `MANIFESTS_DIR` | `"manifests"` | Directory containing `*.json` agent manifests |
| `workspace_dir` | `WORKSPACE_DIR` | `"workspace"` | Sandbox root for file and shell tools |
| `skills_dir` | `SKILLS_DIR` | `"skills"` | Directory containing skill packs |
| `sandbox_python` | `SANDBOX_PYTHON` | `"sandbox/.venv/bin/python"` | Python interpreter for `python_exec`; falls back to `python3` |
| `agent_max_iterations` | `AGENT_MAX_ITERATIONS` | `10` | Max loop iterations when no budget is set |
| `agent_max_tokens_fallback` | `AGENT_MAX_TOKENS_FALLBACK` | `8_192` | `max_tokens` per call when no budget is set |
| `agent_min_iteration_budget_usd` | `AGENT_MIN_ITERATION_BUDGET_USD` | `0.002` | Minimum remaining USD to attempt another iteration |
| `enable_prompt_caching` | `ENABLE_PROMPT_CACHING` | `True` | Inject Anthropic cache headers |
| `capture_events` | `CAPTURE_EVENTS` | `False` | Write events to `workspace/runs/{id}/events.jsonl` |
| `capture_results` | `CAPTURE_RESULTS` | `False` | Write results to `workspace/runs/{id}/results.jsonl` |
| `cost_per_1m_input_tokens` | `COST_PER_1M_INPUT_TOKENS` | `3.0` | USD per 1M input tokens |
| `cost_per_1m_output_tokens` | `COST_PER_1M_OUTPUT_TOKENS` | `15.0` | USD per 1M output tokens |
| `cost_per_1m_cache_write_tokens` | `COST_PER_1M_CACHE_WRITE_TOKENS` | `3.75` | USD per 1M cache-write tokens |
| `cost_per_1m_cache_read_tokens` | `COST_PER_1M_CACHE_READ_TOKENS` | `0.30` | USD per 1M cache-read tokens |
| `max_continuations` | `MAX_CONTINUATIONS` | `3` | Max continuation passes for partial results |
| `file_read_max_lines` | `FILE_READ_MAX_LINES` | `200` | Max lines returned by `file_read` |
| `planner_max_iterations` | `PLANNER_MAX_ITERATIONS` | `15` | Max ReAct iterations for the planner |

### Logging

Configured separately in `logging_config.py` via env vars read in `main.py`:

| Env Var | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `"INFO"` | Log level |
| `LOG_JSON` | `"false"` | Emit JSON-formatted log lines |
| `LOG_FILE` | `None` | Optional file path for log output |

---

## 6. Data Models

All Pydantic schemas are defined in `src/agentflow/core/models.py`.

### MCPServerConfig

```python
class MCPServerConfig(BaseModel):
    name: str
    url: str          # SSE endpoint, e.g. "http://localhost:3001/sse"
    transport: str = "sse"
```

### AgentManifest

```python
class AgentManifest(BaseModel):
    agent_id: str
    version: str = "1.0.0"
    domain: str
    capabilities: list[str] = []
    tools: list[str]                    # allow-list of built-in tool names
    skills: list[str] = []
    mcp_servers: list[MCPServerConfig] = []
    system_prompt: str
    fallback_for: list[str] = []        # agent_ids this agent can substitute for
    max_concurrency: int = 3
    max_iterations: int | None = None   # None → settings.agent_max_iterations
```

### TaskConstraints

```python
class TaskConstraints(BaseModel):
    budget_usd: float | None = None
    timeout_ms: int = 300_000
```

### TaskContext

```python
class TaskContext(BaseModel):
    prior_results: dict[str, Any] = {}
    shared_memory: dict[str, Any] = {}
    prior_messages: list[Any] = Field(default=[], exclude=True)
```

### TaskEnvelope

The unit of work handed to an agent.

```python
class TaskEnvelope(BaseModel):
    task_id: str                          # UUID v4, auto-generated
    parent_run_id: str
    agent_id: str
    instruction: str
    context: TaskContext = TaskContext()
    constraints: TaskConstraints = TaskConstraints()
```

### AgentStatus

```python
class AgentStatus(str, Enum):
    success = "success"
    partial = "partial"   # hit iteration/budget limit; can be continued
    failed  = "failed"
```

### AgentOutput

```python
class AgentOutput(BaseModel):
    structured: dict[str, Any] = {}   # parsed from JSON in the final text block, if any
    text: str = ""
```

### AgentResult

```python
class AgentResult(BaseModel):
    task_id: str
    agent_id: str
    status: AgentStatus
    output: AgentOutput = AgentOutput()
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    tokens_used: int = 0              # sum of all token fields; kept for backward compat
    cost_usd: float = 0.0
    duration_ms: int = 0
    messages: list[Any] = Field(default=[], exclude=True)  # in-memory only
```

### Subtask

```python
class Subtask(BaseModel):
    id: str
    agent_id: str
    instruction: str
    depends_on: list[str] = []
    expected_output: str = ""
    budget_fraction: float | None = None  # share of total run budget
```

### ExecutionPlan

```python
class ExecutionPlan(BaseModel):
    run_id: str
    subtasks: list[Subtask]
```

### SSEEventType

```python
class SSEEventType(str, Enum):
    run_started         = "run:started"
    plan_created        = "plan:created"
    task_dispatched     = "task:dispatched"
    agent_progress      = "agent:progress"
    agent_query         = "agent:query"
    task_complete       = "task:complete"
    task_partial        = "task:partial"
    task_failed         = "task:failed"
    task_continuing     = "task:continuing"
    run_complete        = "run:complete"
    run_error           = "run:error"
    run_budget_exceeded = "run:budget_exceeded"
```

### SSEPayload

```python
class SSEPayload(BaseModel):
    message: str = ""
    partial: Any = None
    data: Any = None
```

### SSEEvent

```python
class SSEEvent(BaseModel):
    run_id: str
    seq: int                            # auto-incremented per emitter
    ts: int                             # milliseconds since epoch
    type: SSEEventType
    agent_id: str | None = None
    payload: SSEPayload = SSEPayload()
```

### RunRequest / RunResponse

```python
class RunRequest(BaseModel):
    task: str
    context: dict[str, Any] = {}
    budget_usd: float | None = None

class RunResponse(BaseModel):
    run_id: str
    status: str = "started"
```

---

## 7. API Endpoints

All routes are mounted on the root `FastAPI` app. Run routes are under the `/api` prefix via an `APIRouter`.

### POST /api/run

- **Request body**: `RunRequest`
- **Response**: `RunResponse`
- **Behaviour**:
  1. Generates a UUID `run_id`.
  2. Starts `engine.run(run_id, task, context, budget_usd)` as a FastAPI `BackgroundTask`.
  3. Polls up to 1 second (20 × 50 ms sleep) for the `StreamEmitter` to appear in `stream_registry`.
  4. Returns `RunResponse` immediately without waiting for the run to complete.

### GET /api/run/{run_id}/stream

- **Response**: `EventSourceResponse` (SSE, `text/event-stream`)
- **Behaviour**:
  1. Looks up the `StreamEmitter` for `run_id` in `stream_registry`.
  2. Raises HTTP 404 if not found.
  3. Returns `EventSourceResponse` wrapping the emitter's async iterator.
  4. Each yielded item is `{"data": "<JSON-serialised SSEEvent>"}`.
  5. The stream ends when the emitter places `None` (sentinel) on its queue.

### GET /health

- **Response**: `{"status": "ok", "agents": [list of agent_id strings]}`
- Registered directly on the root `FastAPI` app (not under `/api`).

---

## 8. Agent System

### Manifest Files

Each agent is described by a JSON file in `manifests/`. The file is loaded at startup into an `AgentManifest` object by `AgentRegistry.load_from_directory()`.

**Registered agents:**

| agent_id | domain | capabilities | tools | skills | max_iterations |
|---|---|---|---|---|---|
| `ResearchAgent` | Information Retrieval | web_research, document_synthesis, citation | web_search, fetch_url, wikipedia, file_read, file_write | — | 8 |
| `CodeAgent` | Software Engineering | **code_generation**, debugging, architecture_review | bash_exec, python_exec, file_read, file_write | python-coding, technical-analysis, frontend-web | 15 |
| `DataAgent` | Data Analysis | statistical_analysis, visualization, anomaly_detection | bash_exec, python_exec, file_read, file_write | python-data-analysis | 12 |
| `WriterAgent` | Content & Communication | copywriting, editing, tone_adaptation, seo | web_search, fetch_url, file_read, file_write | — | 6 |
| `PlannerAgent` | Strategy & Decomposition | task_decomposition, dependency_mapping, risk_analysis | web_search, file_read, file_write | — | 4 |
| `FinancialAnalystAgent` | Financial Analysis | equity_analysis, financial_modeling, ratio_analysis, market_research | web_search, fetch_url, python_exec, file_read, file_write | financial-analysis | 12 |
| `FrontendAgent` | Frontend Web Development | frontend_development, react_development, spa_development, ui_implementation, component_design | bash_exec, file_read, file_write | frontend-web | 15 |

The presence of `code_generation` in `capabilities` is used by the engine as the trigger for decomposer activation.

### AgentRegistry (`core/registry.py`)

```
_agents:            dict[str, AgentManifest]
_capability_index:  dict[str, list[str]]      # capability → agent_ids
```

Key methods:
- `register(manifest)` — stores by `agent_id`, indexes all `capabilities`
- `load_from_directory(directory)` — globs `*.json`, constructs `AgentManifest`
- `get(agent_id) -> AgentManifest | None`
- `all() -> list[AgentManifest]`
- `by_capability(capability) -> list[AgentManifest]`
- `find_fallback(for_agent_id) -> AgentManifest | None` — scans all manifests for `for_agent_id in manifest.fallback_for`
- `summary() -> str` — Markdown roster for the LLM planner listing `agent_id`, `domain`, `capabilities`, `tools`, `skills`

### Agent Class (`agents/agent.py`)

A single generic class. Behaviour is entirely controlled by the manifest.

```python
class Agent:
    def __init__(self, manifest: AgentManifest, client: LLMClient): ...
    async def run(self, envelope: TaskEnvelope, emitter: StreamEmitter,
                  resume_messages: list | None = None) -> AgentResult: ...
    async def _execute(self, envelope, emitter, resume_messages) -> AgentResult: ...
    async def _agentic_loop(self, envelope, tools, system_prompt, emitter,
                            resume_messages) -> AgentResult: ...
    async def _call_tool(self, block, tools, emitter) -> dict: ...
```

#### `Agent.run()`

1. Emits `agent:progress` (starting message).
2. Calls `_execute()`.
3. Catches any unhandled exception → returns `failed` `AgentResult`.
4. Sets `result.duration_ms` from wall clock.

#### `Agent._execute()`

Runs inside an `AsyncExitStack`:

1. Loads local tools from `tool_registry.get_many(manifest.tools)`.
2. If `manifest.skills` is non-empty, injects the `read_skill` tool.
3. For each `MCPServerConfig` in `manifest.mcp_servers`, enters `mcp_session(config)` context → appends discovered MCP tools.
4. Builds `system_prompt = manifest.system_prompt + skill_loader.preamble(skills)`.
5. Calls `_agentic_loop()`.

#### `Agent._agentic_loop()`

**Message initialisation — three paths (evaluated in order):**

| Condition | Behaviour |
|---|---|
| `resume_messages is not None` | Copies existing messages. If last message role is `assistant`, appends a `user` message: "continue from where you left off". |
| `envelope.context.prior_messages` is non-empty | Copies prior messages (single-dependency chain); appends new `user` message with the instruction. |
| Standard path | Builds `user` message from `instruction` + JSON `<context>` block if `prior_results` is non-empty. |

**Per-iteration budget / limit check:**

| Condition | Action |
|---|---|
| `task_budget` is set and `remaining <= settings.agent_min_iteration_budget_usd` | Sets `hit_limit = True`, breaks. |
| `task_budget` is set and sufficient | Computes `max_tokens` via `_budget_to_max_tokens(remaining, last_input_tokens)`, clamped to `[256, 16_384]`. |
| No budget | Checks `iteration >= max_iterations`; uses `settings.agent_max_tokens_fallback`. |

**Per-iteration API call:**

- `client.messages.create(model, max_tokens, system, messages, tools?)` — `tools` is omitted from kwargs entirely when the list is empty.
- Accumulates `input_tokens`, `output_tokens`, `cache_creation_tokens`, `cache_read_tokens`, `cost_usd`.
- Appends full `response.content` (preserving `tool_use` blocks) as `assistant` message.

**Stop conditions:**

| `stop_reason` | Action |
|---|---|
| `end_turn` | Breaks → `status=success` |
| `max_tokens` | Sets `hit_limit = True`, breaks → `status=partial` |
| `tool_use` | Gathers all tool-use blocks, executes concurrently via `asyncio.gather(_call_tool(...))`, appends results as `user` message, continues loop |

**Return value:**
- `status = partial` if `hit_limit`, else `success`
- Tries `json.loads(final_text)` for `structured` field; silently falls back to `{}` on failure.
- Sets `messages = messages` for downstream continuation.

#### `Agent._call_tool()`

1. Emits `agent:progress` with tool name and input.
2. Looks up tool: if in `tool_registry` → `tool_registry.execute(name, input)`, else if in local `tools` list → `tool_def.handler(**input)`, else returns error string.
3. Truncates result to 8,000 characters.
4. Returns `{"type": "tool_result", "tool_use_id": block.id, "content": result_text}`.

#### `_budget_to_max_tokens(remaining_budget, last_input_tokens) -> int`

```
estimated_input_cost = last_input_tokens * cost_per_1m_input / 1_000_000
output_budget = remaining - estimated_input_cost
max_tokens = int(output_budget / (cost_per_1m_output / 1_000_000))
return clamp(max_tokens, 256, 16_384)   # returns 256 if output_budget <= 0
```

---

## 9. Tool System

### ToolImpact

```python
class ToolImpact(str, Enum):
    read_only = "read_only"   # no side effects
    write     = "write"       # creates or modifies files
    execute   = "execute"     # runs arbitrary code or shell commands
```

### ToolDefinition

```python
@dataclass
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]               # JSON Schema object
    handler: Callable[..., Awaitable[str]]
    impact: ToolImpact = ToolImpact.read_only

    def to_anthropic_param(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": self.input_schema}
```

### ToolRegistry

```python
class ToolRegistry:
    _tools: dict[str, ToolDefinition]

    def register(self, tool: ToolDefinition) -> None
    def get(self, name: str) -> ToolDefinition | None
    def get_many(self, names: list[str]) -> list[ToolDefinition]   # silently skips unknowns
    def all(self) -> list[ToolDefinition]
    async def execute(self, name: str, input_data: dict) -> str    # catches TypeError + Exception
```

Global singleton: `tool_registry` at module level in `tools/registry.py`.

**Registration side effect:** `tools/__init__.py` imports `builtin` and `skills` modules, triggering all `tool_registry.register()` calls. Any module that imports from `agentflow.tools` gets all built-ins registered.

### Built-in Tools

All file-system tools resolve paths relative to `_workspace()` (`settings.workspace_dir`, auto-created). Path traversal is blocked by `_safe_path()`, which rejects paths that escape the workspace root.

#### `fetch_url`
- Impact: `read_only`
- Parameters: `url: str`
- Uses `httpx.AsyncClient(follow_redirects=True, timeout=15)`.
- Returns up to 8,000 characters of response body.

#### `web_search`
- Impact: `read_only`
- Parameters: `query: str`, `max_results: int = 5`
- DuckDuckGo Instant Answers API: `https://api.duckduckgo.com/?q=...&format=json`
- Returns abstract + related topics.

#### `wikipedia`
- Impact: `read_only`
- Parameters: `topic: str`
- Wikipedia REST API: `https://en.wikipedia.org/api/rest_v1/page/summary/{topic}`
- Returns title + extract.

#### `file_read`
- Impact: `read_only`
- Parameters: `path: str`, `start_line: int?`, `end_line: int?`, `pattern: str?` (regex), `context_lines: int = 5`, `max_lines: int?`, `include_line_numbers: bool = True`
- With `pattern`: returns matching lines + `context_lines` context, with `--- match at line N ---` separators.
- Without `pattern`: returns `[from_line=X, to_line=Y, total_lines=Z]` header + lines; truncated at limit with a hint (`use start_line=N to read more`).

#### `file_write`
- Impact: `write`
- Parameters: `path: str`, `content: str`, `mode: str = "overwrite"`, `line: int?`, `start_line: int?`, `end_line: int?`, `pattern: str?`, `start_pattern: str?`, `end_pattern: str?`
- Modes: `overwrite`, `append`, `insert_at_line` (requires `line`), `replace_lines` (requires `start_line`, `end_line`), `replace_pattern` (requires `pattern`), `replace_between` (requires `start_pattern`, `end_pattern`).

#### `bash_exec`
- Impact: `execute`
- Parameters: `command: str`, `purpose: str`, `timeout_seconds: int = 30`
- Runs via `asyncio.create_subprocess_shell`, cwd = workspace.
- Returns `exit_code=N\n<stdout+stderr>`, truncated to 8,000 characters.

#### `python_exec`
- Impact: `execute`
- Parameters: `code: str`, `purpose: str`, `timeout_seconds: int = 30`
- Runs via `asyncio.create_subprocess_exec(_sandbox_python(), "-c", code)`.
- `_sandbox_python()` tries `settings.sandbox_python`; falls back to `python3`.
- Returns `exit_code=N\n<stdout+stderr>`, truncated to 8,000 characters.

#### `read_skill`
- Impact: `read_only`
- Parameters: `skill: str`, `topic: str = "general"`
- Delegates entirely to `skill_loader.read(skill, topic)`.

### Stub Tools

12 stubs are registered at the bottom of `builtin.py`. Each returns an "not yet integrated" message. Names: `arxiv_search`, `sql_query`, `chart_gen`, `csv_parse`, `spell_check`, `readability_score`, `tone_analyzer`, `dependency_graph`, `timeline_gen`, `risk_model`, `lint`, `test_runner`.

### MCP Tool Integration (`tools/mcp_tools.py`)

`mcp_session(config: MCPServerConfig)` is an async context manager:

1. Connects via `mcp.client.sse.sse_client(config.url)` → `(read, write)` streams.
2. Creates `ClientSession(read, write)`, calls `session.initialize()`, `session.list_tools()`.
3. Wraps each discovered tool as a `ToolDefinition` with a handler that calls `session.call_tool(tool_name, kwargs)`.
4. On any error (connection failure or `mcp` not installed), yields an empty list and logs a warning.

---

## 10. Orchestration Engine

`OrchestratorEngine` in `orchestrator/engine.py`.

### Initialisation

```python
class OrchestratorEngine:
    def __init__(self, registry: AgentRegistry):
        self._registry = registry
        self._client = LLMClient(enable_caching=settings.enable_prompt_caching)
        self._agent_instances = self._build_agents()  # one Agent per manifest
```

### `engine.run(run_id, task, user_context, budget_usd)`

The complete 7-step pipeline:

```
1. Create StreamEmitter  →  stream_registry.create(run_id, events_file)
2. Create RunContext     →  context_store.create(run_id, results_file, budget_usd)
3. Create bus channels   →  task_bus.create_run(run_id)
4. Emit run:started
5. create_plan(...)      →  ExecutionPlan
6. expand_plan(...)      →  ExecutionPlan (coding subtasks expanded)
7. Emit plan:created
8. _execute_plan(...)    →  schedules and dispatches all subtasks
9. Emit run:complete (or run:error on exception)
finally:
   client.stats.log_summary()
   emitter.close()
   task_bus.close_run(run_id)
   context_store.remove(run_id)
```

### `_execute_plan(run_id, plan, ctx, emitter)`

Scheduling loop:

```python
graph = DependencyGraph(plan)
completed: set[str] = set()
failed:    set[str] = set()
in_flight: dict[str, asyncio.Task] = {}

while len(completed) + len(failed) < len(plan.subtasks):
    ready = graph.ready(completed, failed)
    for subtask in ready:
        if subtask.id not in in_flight:
            budget = _compute_subtask_budget(subtask, plan, completed, failed, in_flight, ctx)
            task = asyncio.create_task(_dispatch_subtask(..., budget))
            in_flight[subtask.id] = task

    if not in_flight:   # stuck: upstream failures block all remaining subtasks
        for pending in remaining_subtasks:
            emit task:failed (upstream dependency failed)
        break

    done, _ = await asyncio.wait(in_flight.values(), return_when=FIRST_COMPLETED)
    for t in done:
        subtask_id = ...
        del in_flight[subtask_id]
        if t.result(): completed.add(subtask_id)
        else:          failed.add(subtask_id)
```

### `_dispatch_subtask(run_id, subtask, ctx, emitter, task_budget_usd) -> bool`

1. Looks up agent instance; missing agent → emit `task:failed`, return `False`.
2. Builds `prior_results` and `prior_messages` from context (via `ctx.build_prior_results()` and `ctx.build_prior_messages()`).
3. Constructs `TaskEnvelope`.
4. Emits `task:dispatched`.
5. **Retry loop** (`1 .. task_max_retries + 1`):
   - `asyncio.wait_for(agent.run(envelope, emitter), timeout=task_timeout_ms/1000)`
   - `TimeoutError` → exponential backoff (`2^attempt` seconds) → retry or fail.
   - `status == failed` → backoff → retry; on final attempt, check `registry.find_fallback()` and try fallback agent.
   - `status == partial` with no task budget → call `_continue_partial()`.
   - On success/partial: `ctx.store_result()`, emit `task:complete` or `task:partial`, return `True`.
   - On final failure: emit `task:failed`, return `False`.

### `_continue_partial(run_id, subtask, envelope, result, ctx, emitter) -> AgentResult`

```python
for _ in range(settings.max_continuations):
    if not ctx.within_budget():
        emit run:budget_exceeded
        return result
    emit task:continuing
    result = await agent.run(envelope, emitter, resume_messages=result.messages)
    ctx.add_result_cost(result)
    if result.status != partial:
        break
return result
```

### `_compute_subtask_budget(subtask, plan, completed, failed, in_flight, ctx)`

```python
if ctx.budget_usd is None or subtask.budget_fraction is None:
    return None
remaining = ctx.remaining_budget_usd()
pending = [s for s in plan.subtasks
           if s.id not in completed and s.id not in failed and s.id not in in_flight]
total_pending_fraction = sum(s.budget_fraction or 0 for s in pending)
if total_pending_fraction == 0:
    return remaining
return remaining * (subtask.budget_fraction / total_pending_fraction)
```

---

## 11. Planning System

### LLM Planner (`orchestrator/planner.py`)

`create_plan(run_id, task, registry, client, budget_usd=None) -> ExecutionPlan`

**System prompt** = `_SYSTEM_PROMPT_BASE` always + `_BUDGET_ALLOCATION_INSTRUCTIONS` when `budget_usd` is set.

The system prompt instructs the model to:
- Run an exploration phase using up to 5–8 tool calls (`file_read`, `bash_exec`, `web_search`, `fetch_url` — **no writes**).
- Produce a final JSON plan with fields `subtasks[].{id, agentId, instruction, dependsOn, expectedOutput}` (plus `budgetFraction` when budget is set).
- Follow task-scope rules: single subtask if completable in ≤15 tool calls and ≤3 files; otherwise split.
- Follow parallelism rules: minimise `depends_on`, prefer breadth over depth.
- Add a verification subtask after each file-generating subtask.

Planner allowed tools: `["file_read", "bash_exec", "web_search", "fetch_url"]`.

**ReAct loop** (up to `settings.planner_max_iterations`):

```
while iteration < max_iterations:
    response = client.messages.create(model=planner_model, max_tokens=4096, ...)
    if stop_reason == "end_turn": break
    if stop_reason == "tool_use":
        results = await asyncio.gather(*[_call_planner_tool(b, tools) for b in tool_use_blocks])
        # tool results capped at _MAX_TOOL_RESULT_CHARS = 8_000
        append results, continue
```

**JSON parsing:**
- Extracts final `TextBlock` from last response.
- Strips markdown code fences.
- Parses via `json.loads`.
- On failure, falls back to a single subtask assigned to the first registered agent.

**Budget normalisation** (when `budget_usd` set):
- If all fractions are zero → assign `1.0/n` equally.
- If fractions don't sum to ~1.0 → renormalise by dividing each by total.

### Decomposer (`orchestrator/decomposer.py`)

`expand_plan(plan, coding_agent_ids: set[str], client) -> ExecutionPlan`

Expands subtasks assigned to agents with `code_generation` capability:
- Non-coding subtasks pass through unchanged.
- Each coding subtask is sent to `decompose_coding_subtask()`.
- Tracks `tail_id: dict[str, str]` mapping original subtask id → last micro-subtask id.
- Rewires downstream `depends_on` references from original ids to tail ids.

`decompose_coding_subtask(subtask, client) -> list[Subtask]`
- Single LLM call (`planner_model`, `max_tokens=2048`) with `_SYSTEM_PROMPT`.
- Returns `[subtask]` unchanged if decomposition yields a single element.
- Splits `budget_fraction` equally across micro-subtasks.
- Sets `depends_on` on the first micro-subtask from the original subtask's `depends_on`; subsequent micro-subtasks depend on the previous one.

---

## 12. Scheduling

`DependencyGraph` in `orchestrator/scheduler.py`.

```python
class DependencyGraph:
    def __init__(self, plan: ExecutionPlan):
        # builds nx.DiGraph: edge dep → st for each (st, dep) pair
        # raises ValueError if not a DAG

    def ready(self, completed: set[str], failed: set[str]) -> list[Subtask]:
        # returns subtasks where all predecessors are in completed
        # subtasks with any predecessor in failed are never returned

    def topological_order(self) -> list[str]:
        # nx.topological_sort
```

---

## 13. SSE Streaming

### StreamEmitter (`orchestrator/stream.py`)

```python
class StreamEmitter:
    run_id: str
    _queue: asyncio.Queue[SSEEvent | None]
    _seq: int
    _events_file: str | None

    def emit(self, event_type, *, agent_id=None, message="", data=None):
        # constructs SSEEvent with auto-incremented seq and current timestamp
        # queue.put_nowait(event)
        # optionally appends JSON line to events_file

    def close(self):
        # queue.put_nowait(None)  — sentinel

    async def __aiter__(self):
        # yields {"data": json.dumps(event.model_dump(mode="json"))}
        # stops on None sentinel
```

The dict shape `{"data": "..."}` is what `sse-starlette`'s `EventSourceResponse` expects. The SSE wire format is `data: <json>\n\n` per event.

### StreamRegistry

Global singleton `stream_registry`. Stores `dict[str, StreamEmitter]`.

Methods: `create(run_id, events_file) -> StreamEmitter`, `get(run_id) -> StreamEmitter | None`, `remove(run_id)`.

### Event Lifecycle

```
POST /api/run
  → engine creates StreamEmitter, stores in stream_registry
  → returns run_id

GET /api/run/{run_id}/stream
  → EventSourceResponse(stream_registry.get(run_id))
  → reads from asyncio.Queue as events arrive

engine emits:  run:started → plan:created → task:dispatched → agent:progress
               → task:complete | task:partial | task:failed → run:complete | run:error
StreamEmitter.close()  →  sentinel None on queue  →  SSE connection closed
```

---

## 14. LLM Client

`LLMClient` in `llm/client.py`. Wraps `anthropic.AsyncAnthropic`.

### Interface

```python
class LLMClient:
    messages: _MessagesProxy
    stats: UsageStats
```

### `_MessagesProxy.create(**kwargs)`

1. Looks up or creates a `RateLimiter` for the model.
2. If `enable_caching`: pops `system` and `tools` from kwargs, calls `_apply_caching()`, reinjects.
3. `await limiter.acquire()`.
4. `await self._inner.messages.create(**kwargs)`.
5. On error: `limiter.release_failed()`, re-raise.
6. On success: `limiter.record(input_tokens, output_tokens)`, updates `UsageStats`.

### Prompt Caching (`_apply_caching(system, tools)`)

| Input | Transformation |
|---|---|
| `system` is `str` | Wrapped in `[{"type": "text", "text": ..., "cache_control": {"type": "ephemeral"}}]` |
| `system` is `list` | `cache_control` set on the last element |
| `tools` is `list` | `cache_control` set on the last tool dict |

This places one cache breakpoint on the full system prompt and one on the full tool list.

### RateLimiter

Sliding 60-second window using a `deque[_WindowEntry]`. Three limits:
- Requests per minute (`_rpm`)
- Input tokens per minute (`_itpm`)
- Output tokens per minute (`_otpm`)

In-flight counter incremented at `acquire()`, decremented at `record()` or `release_failed()`.

Defaults for `claude-sonnet-4-6` and `claude-haiku-4-5-20251001`: 50 rpm, 30,000 itpm, 8,000 otpm.

### UsageStats

```python
class UsageStats:
    total_requests: int
    total_input_tokens: int
    total_output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int

    @property
    def cache_hit_rate(self) -> float:
        return cache_read / (total_input + cache_read)

    def log_summary(self): ...
```

---

## 15. Context and Budget System

`RunContext` and `ContextStore` in `core/context.py`.

### RunContext

Per-run state. Created at run start, destroyed in `engine.run`'s `finally` block.

```python
class RunContext:
    run_id: str
    budget_usd: float | None
    _results: dict[str, AgentResult]
    _lock: asyncio.Lock
    _results_file: str | None
    _total_cost_usd: float

    def add_result_cost(self, result: AgentResult) -> None
    def total_cost_usd(self) -> float
    def remaining_budget_usd(self) -> float | None   # max(0, budget - spent) or None
    def within_budget(self) -> bool                   # True if no budget or under limit

    async def store_result(self, subtask_id, result)  # locked; optionally writes JSONL
    async def get_result(self, subtask_id) -> AgentResult | None
    async def all_results(self) -> dict[str, AgentResult]

    async def build_prior_results(self, dep_ids: list[str]) -> dict[str, Any]
    # returns {dep_id: result.output.text or str(structured)}

    async def build_prior_messages(self, dep_ids: list[str]) -> list[Any]
    # returns prior messages only when exactly one dependency; empty list otherwise
```

### ContextStore

Global singleton `context_store`. Maps `run_id → RunContext`. Methods: `create`, `get`, `remove`.

---

## 16. Skill System

`SkillLoader` in `core/skill_loader.py`.

### File Structure

```
skills/
└── {skill-name}/
    ├── SKILL.md            # required; YAML frontmatter + Markdown body
    └── {topic}.md          # optional reference documents
```

SKILL.md frontmatter format:
```yaml
---
name: skill-name
description: One-line description
---
```

### Validation Rules

- Skill name: matches `^[a-z0-9-]+$`
- Topic name: matches `^[a-zA-Z0-9._-]+$` and does not contain `".."`

### Methods

```python
class SkillLoader:
    def frontmatter(self, skill_name: str) -> dict[str, str]
    def description(self, skill_name: str) -> str   # from frontmatter or first body line
    def name(self, skill_name: str) -> str           # warns if mismatch with folder name
    def read(self, skill_name: str, topic: str = "general") -> str
    # reads SKILL.md for topic="general", else reads {topic}.md
    def preamble(self, skill_names: list[str]) -> str
    # generates "## Available Skills" section appended to system_prompt
```

Global singleton: `skill_loader` using `settings.skills_dir`.

---

## 17. Task Bus

`TaskBus` in `core/bus.py`.

Two asyncio queues per run: dispatch (orchestrator → agent) and result (agent → orchestrator).

The bus is created and torn down per-run. The engine calls `task_bus.create_run()` and `task_bus.close_run()` for lifecycle management but does **not** actually enqueue or dequeue tasks through it — the engine dispatches subtasks directly via `asyncio.create_task(agent.run(...))`.

**The bus is infrastructure for a future Redis Streams migration.** The API (`enqueue_task`, `dequeue_task`, `task_done`, `publish_result`, `consume_result`) is a drop-in point for async worker scaling.

Global singleton: `task_bus`.

---

## 18. Reporter

`compile_report()` in `orchestrator/reporter.py`.

```
compile_report(run_id, task, plan, all_results, client, cost_summary) -> str
```

1. Identifies "leaf" subtask IDs: those not in any other subtask's `depends_on`.
2. Filters to leaf-node successes for synthesis; falls back to all successes if none are leaves.
3. Builds synthesis input: task header + each result (capped at 8,000 chars) + partial section + failed section.
4. Single LLM call (`reporter_model`, `max_tokens=2048`) with `_SYNTHESIS_PROMPT`.
5. Builds Markdown report: header (task, timestamp, run_id, cost) + LLM body.
6. Writes to `workspace/runs/{run_id}/report.md`.
7. Returns the file path, included in the `run:complete` event's `data.report` field.

---

## 19. CLI

### Commands (`cli/__init__.py`)

Global options: `--host` (default `127.0.0.1`, env `AGENTFLOW_HOST`), `--port` (default `8001`, env `AGENTFLOW_PORT`).

#### `agentflow run <task>`
- Options: `--context KEY=VALUE` (repeatable), `--verbose / -v`, `--json`
- Calls `start_run(base_url, task, ctx_dict)` → `run_id`
- Calls `stream_events(base_url, run_id)` → async generator of event dicts
- Passes each event to `RunDisplay.handle_event()`
- Breaks on `run:complete` or `run:error`

#### `agentflow serve`
- Options: `--reload`
- Calls `uvicorn.run("agentflow.main:app", host, port, reload=reload)`

#### `agentflow health`
- Calls `check_health(base_url)` → prints agent list

### CLI Client (`cli/client.py`)

```python
async def check_health(base_url: str) -> dict
async def start_run(base_url: str, task: str, context: dict) -> str   # returns run_id
async def stream_events(base_url: str, run_id: str) -> AsyncGenerator[dict, None]
# httpx.Timeout(None), parses "data: " prefixed lines
```

### RunDisplay (`cli/display.py`)

Rich console renderer. Icon and style mapped per event type.

| Event | Rendering |
|---|---|
| `plan:created` | Rich Table: ID, Agent, Instruction (truncated to 60 chars), Depends on |
| `run:complete` | Message + Rich Panel per subtask output |
| `agent:progress` | Shown only with `--verbose` |

---

## 20. Dependencies

From `pyproject.toml`. Requires Python `>=3.13`.

| Package | Version | Purpose |
|---|---|---|
| `anthropic` | `>=0.101.0` | Claude API client |
| `click` | `>=8.1.0` | CLI framework |
| `fastapi` | `>=0.136.1` | HTTP server framework |
| `httpx` | `>=0.28.1` | Async HTTP client (tools + CLI) |
| `mcp` | `>=1.27.1` | MCP client for remote tool servers |
| `networkx` | `>=3.6.1` | DAG construction and topological sort |
| `pydantic` | `>=2.13.4` | Data validation |
| `pydantic-settings` | `>=2.14.1` | Settings from env vars |
| `python-dotenv` | `>=1.2.2` | `.env` file loading |
| `rich` | `>=13.0.0` | Terminal output |
| `sse-starlette` | `>=3.4.4` | SSE for FastAPI |
| `uvicorn` | `>=0.46.0` | ASGI server |

Dev: `pytest>=9.0.3`, `pytest-asyncio>=1.3.0`. Pytest config: `asyncio_mode = "auto"`.

Entry point: `agentflow = "agentflow.cli:main"`. Build backend: `uv_build>=0.9.21,<0.10.0`.

---

## 21. Key Design Decisions

### Manifest-Driven Agent Polymorphism

A single `Agent` class handles all agent types. Behaviour is entirely controlled by the JSON manifest. Adding a new agent requires zero Python code — drop a manifest file and restart. The `AgentRegistry.summary()` method generates a plain-text roster that the LLM planner uses to assign subtasks.

### Three-Path Message Initialisation

The agentic loop chooses message initialisation at runtime:

1. **Continuation** (`resume_messages`): used by `_continue_partial`. Resumes an existing thread; appends "continue from where you left off" if the last turn was assistant.
2. **Single-dependency chain** (`prior_messages`): used when a subtask has exactly one upstream dependency. Inherits the full message thread, avoiding redundant file reads.
3. **Standard** (`prior_results`): fresh start with instruction + JSON context block. Used for independent subtasks or those with multiple dependencies.

### Budget as Primary Limiter

When `budget_usd` is set, it propagates:

```
RunRequest.budget_usd
  → RunContext.budget_usd
  → _compute_subtask_budget()  (proportional allocation across pending subtasks)
  → TaskConstraints.budget_usd
  → Agent._agentic_loop()  (per-iteration max_tokens via _budget_to_max_tokens)
```

The `max_iterations` limit becomes secondary. Budget-driven runs are more predictable in cost.

### Prompt Caching as a Transparent Cross-Cutting Concern

`LLMClient` injects `cache_control: {"type": "ephemeral"}` on the last system block and last tool definition on every API call. No caller code is aware of this. The same `LLMClient` instance is shared by the planner, decomposer, all agents, and the reporter.

### Partial Result Continuation

`_continue_partial` resumes a partial subtask up to `max_continuations` times using the **existing message thread** (`result.messages`) rather than rebuilding context from text summaries. This preserves all tool results already accumulated in the conversation.

### Fallback Agent Chain

The orchestrator calls `registry.find_fallback(agent_id)` only after all retry attempts are exhausted. The fallback receives the same `TaskEnvelope` with a swapped `agent_id`.

### Dependency Cancellation Semantics

`DependencyGraph.ready()` never returns a subtask whose predecessor is in `failed`. The scheduling loop detects "nothing in flight and not all done" and emits `task:failed` for all still-pending subtasks with an "upstream dependency failed" message.

### Tool Result Truncation

Both the agent and the planner cap tool results at 8,000 characters before appending them to the message thread. This prevents large file reads or web fetches from dominating context across subsequent iterations.

### TaskBus Exists but is Unused in the Current Dispatch Path

The engine calls `task_bus.create_run()` and `task_bus.close_run()` for lifecycle, but dispatches subtasks directly via `asyncio.create_task(agent.run(...))`. The bus API (`enqueue_task`, `dequeue_task`, etc.) is a pre-wired drop-in point for Redis Streams if async worker scaling across processes is needed.

---

## 22. Known Issues and Future Work

### Stale Test Assertion

`tests/test_models.py::test_task_envelope_defaults` asserts `env.constraints.max_tokens == 4096`. `TaskConstraints` no longer has a `max_tokens` field — it has `budget_usd: float | None` and `timeout_ms: int`. This test fails against the current codebase and must be updated.

### Stub Tools

12 tools (`arxiv_search`, `sql_query`, `chart_gen`, `csv_parse`, `spell_check`, `readability_score`, `tone_analyzer`, `dependency_graph`, `timeline_gen`, `risk_model`, `lint`, `test_runner`) are registered but return "not yet integrated" messages. Agents that attempt to use these will receive non-functional responses.

### TaskBus Migration Path

Replace `core/bus.py`'s asyncio queues with Redis Streams and run agent workers as separate processes. The `enqueue_task`/`consume_result` interface is pre-designed for this.

### Rate Limiter Defaults

The rate limiter defaults (50 rpm, 30,000 itpm, 8,000 otpm) are hardcoded for `claude-sonnet-4-6` and `claude-haiku-4-5-20251001`. Other models fall back to the same defaults. These should be configurable or automatically fetched from the Anthropic API.

### Single `LLMClient` Instance

All orchestrator components share a single `LLMClient`. The rate limiter is per-model, but there is no per-component isolation — a burst from one agent's tool calls can starve the planner.

### Event Persistence

`capture_events` and `capture_results` are off by default. There is no mechanism to replay or resume a run from persisted events.

### No Authentication on API

`POST /api/run` and `GET /api/run/{id}/stream` have no authentication or authorisation. The server is intended for local use only.
