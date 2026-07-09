---
title: Codebase Map
last_updated: 2026-07-09
last_verified_sha: 1b92446
sources:
  - src/agentflow/
  - manifests/
  - skills/
  - tests/
status: current
---

# Codebase Map

Where things live in `src/agentflow/`, `manifests/`, `skills/`, and `tests/`. See
[architecture](architecture.md) for how the pieces fit together at runtime.

## Package layout

| Package | Responsibility | Key files |
| --- | --- | --- |
| [core](../../src/agentflow/core/) | Shared domain models, per-run state, agent-manifest loading, and skill discovery — no orchestration logic. | `models.py` (all Pydantic models: manifests, `Subtask`/`ExecutionPlan`, `AgentResult`, SSE events), `context.py` (`RunContext`/`ContextStore` + `_make_context_store()` backend factory), `context_redis.py` (`RedisRunContext`/`RedisContextStore`), `bus.py` (`TaskBus`, in-process dispatch/result queues, backend factory), `bus_redis.py` (`RedisTaskBus`), `redis_client.py` (shared async Redis pool), `registry.py` (`AgentRegistry` — loads `manifests/*.yaml`), `skill_loader.py` (reads `skills/*/SKILL.md` + reference docs) |
| [agents](../../src/agentflow/agents/) | The single generic, manifest-driven agent runtime shared by every agent type. | `agent.py` (`Agent` class — tool-calling `_agentic_loop()` against the LLM, returns `AgentResult`) |
| [orchestrator](../../src/agentflow/orchestrator/) | Turns one task into a plan, schedules/executes it as a DAG, and reports the outcome. | `engine.py` (`OrchestratorEngine`, top-level lifecycle), `planner.py` (`create_plan()`), `decomposer.py` (`expand_plan()`), `scheduler.py` (`DependencyGraph`), `reporter.py` (`compile_report()`), `stream.py` (`StreamEmitter`/`StreamRegistry` for SSE + backend factory), `stream_redis.py` (`RedisStreamEmitter`/`RedisStreamRegistry`, Redis Streams) |
| [llm](../../src/agentflow/llm/) | Single point of contact with the Anthropic API. | `client.py` (`LLMClient` — wraps `AsyncAnthropic.messages.create()`, prompt-cache injection, `UsageStats`; retries delegated to the SDK's `max_retries`) |
| [tools](../../src/agentflow/tools/) | Tool definitions agents can call, plus the registry they're looked up in. | `registry.py` (`ToolDefinition`/`ToolImpact`/`tool_registry`), `builtin.py` (built-ins: `bash_exec`, `python_exec`, `file_read`, `file_write`, `web_search`, `fetch_url`, …), `mcp_tools.py` (wraps remote MCP servers as `ToolDefinition`s), `skills.py` (`read_skill` tool), `arxiv_search.py` (arXiv API client), `artifact_tracker.py` (per-run `artifacts.jsonl` writer) |
| [api](../../src/agentflow/api/) | HTTP surface. | `routes.py` (`POST /api/runs`, `GET /api/runs/{run_id}/stream` SSE, past-run query endpoints) |
| [cli](../../src/agentflow/cli/) | Terminal client for the API. | `__init__.py` (`main()` entry point), `client.py` (async HTTP client), `display.py` (Rich terminal rendering of SSE events) |

## Top-level modules

- [`main.py`](../../src/agentflow/main.py) — FastAPI app factory; builds the `AgentRegistry`, `OrchestratorEngine`, and CORS/router wiring once at import time. A `lifespan` handler closes the shared Redis pool on shutdown when the Redis backend is active.
- [`config.py`](../../src/agentflow/config.py) — `Settings` (pydantic-settings): API key, model names per role (`planner_model`, `agent_model`, `reporter_model`), timeouts, `manifests_dir`, the state backend (`state_backend`, `redis_url`, `redis_key_ttl`), etc., loaded from `.env`.
- [`logging_config.py`](../../src/agentflow/logging_config.py) — `setup_logging()`; centralized logging setup (level, optional JSON format, optional file sink).
- [`__main__.py`](../../src/agentflow/__main__.py) — `python -m agentflow` entry point; delegates to `agentflow.cli.main()`.

## Manifests

[`manifests/*.yaml`](../../manifests/) each describe one `AgentManifest`
(`agent_id`, `domain`, `capabilities`, `tools`, `skills`, `mcp_servers`, `system_prompt`,
optional `decomposition_prompt`) loaded by `AgentRegistry` at startup. Present manifests:
`business_analyst_agent.yaml`, `code_agent.yaml`, `data_agent.yaml`,
`financial_analyst_agent.yaml`, `frontend_agent.yaml`, `knowledgebase_agent.yaml`,
`planner_agent.yaml`, `research_agent.yaml`, `writer_agent.yaml`.

## Skills

`skills/` holds the AgentFlow agent skills — per-domain reference material (`SKILL.md` plus
supporting topic docs) that agents pull in on demand via the `read_skill` tool
(`src/agentflow/tools/skills.py`), loaded through `core/skill_loader.py`. A manifest opts an
agent into a skill via its `skills:` list. Skill directories present: `business-analysis`,
`equity-research`, `financial-analysis`, `frontend-web`, `python-coding`,
`python-data-analysis`, `technical-analysis`.

## Tests

`tests/` is a flat pytest suite (no subpackages): `test_agent.py`, `test_arxiv_search.py`,
`test_models.py`, `test_registry.py`, `test_scheduler.py`, `test_skill_loader.py`,
`test_tools.py`. `asyncio_mode = "auto"` is set in `pyproject.toml`, so async test functions
run without extra markers. Run the suite with:

```bash
uv run pytest
```

## Related

- [architecture](architecture.md) — end-to-end request lifecycle and component responsibilities.
- [concepts](concepts.md) — domain glossary for the model types referenced above.
- [subsystems/redis-backend](subsystems/redis-backend.md) — how the `*_redis.py` modules and backend factories fit together.
