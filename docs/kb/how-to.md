---
title: How-To Recipes
last_updated: 2026-07-09
last_verified_sha: 517f320
sources:
  - manifests/
  - src/agentflow/core/registry.py
  - src/agentflow/tools/registry.py
  - src/agentflow/tools/skills.py
  - src/agentflow/core/skill_loader.py
status: current
---

# How-To Recipes

## Add a new agent

1. Create a manifest file in `manifests/`, e.g. `manifests/my_agent.yaml` (`.json`/`.yml` also accepted — `AgentRegistry.load_from_directory` in `src/agentflow/core/registry.py` globs `*.json`, `*.yaml`, `*.yml`).
2. Set the required fields (see `AgentManifest` in `src/agentflow/core/models.py`):
   - `agent_id` (str, must be unique — used as the registry dict key)
   - `domain` (str)
   - `system_prompt` (str)
3. Set the optional fields as needed (defaults shown):
   - `version` (default `"1.0.0"`)
   - `capabilities` (list, default `[]`) — feeds `AgentRegistry.by_capability()` and is shown to the LLM planner via `AgentRegistry.summary()`
   - `tools` (list, default `[]`) — allow-list of tool names; must already be registered in `tool_registry` (see "Add a new tool" below)
   - `skills` (list, default `[]`) — skill folder names under `skills/` (see "Attach a skill to an agent")
   - `mcp_servers` (list of `MCPServerConfig`, default `[]`)
   - `decomposition_prompt` (str, optional — used by `orchestrator/decomposer.py` to split large subtasks)
   - `fallback_for` (list, default `[]`) — agent_ids this agent substitutes for on failure (`AgentRegistry.find_fallback`)
   - `max_concurrency` (int, default `3`)
   - `max_iterations` (int or `None`, default `None` — `None` falls back to `settings.agent_max_iterations`)
4. No code change is required to register the agent. `src/agentflow/main.py` builds one `AgentRegistry()` at import time and calls `registry.load_from_directory(settings.manifests_dir)` (default `manifests/`, see `src/agentflow/config.py`), so the new manifest is picked up on the next process start (`uv run uvicorn agentflow.main:app --reload` or `agentflow serve`).
5. A manifest that fails Pydantic validation, or shares its filename stem with an already-loaded manifest, is skipped with a logged error/warning — the process still starts with the remaining agents.

## Add a new tool

1. Implement an `async def` handler in `src/agentflow/tools/builtin.py` (or a new module under `src/agentflow/tools/`) with the signature `async def _my_tool(**named_params) -> str`. Return a string — this becomes the tool result the agent sees.
2. Register it at import time:
   ```python
   from agentflow.tools.registry import ToolDefinition, ToolImpact, tool_registry

   tool_registry.register(ToolDefinition(
       name="my_tool",
       description="One-line description shown to the LLM.",
       input_schema={
           "type": "object",
           "properties": {"arg": {"type": "string", "description": "..."}},
           "required": ["arg"],
       },
       handler=_my_tool,
       impact=ToolImpact.read_only,  # or ToolImpact.write / ToolImpact.execute
   ))
   ```
3. If you added a new module (not `builtin.py`/`skills.py`), import it in `src/agentflow/tools/__init__.py` alongside the existing `import agentflow.tools.builtin` / `import agentflow.tools.skills` lines — registration only happens when the module is imported.
4. Add the tool's `name` to the `tools:` list of every agent manifest that should use it. `Agent` (`src/agentflow/agents/agent.py`) calls `tool_registry.get_many(self.manifest.tools)` to build the per-agent tool set, so a registered-but-unlisted tool is invisible to that agent.
5. `ToolRegistry.execute()` (`src/agentflow/tools/registry.py`) catches handler exceptions and returns an error string to the agent rather than raising — no extra error handling is required in the handler for that case.

## Attach a skill to an agent

1. Create a folder under `skills/`, e.g. `skills/my-skill/` (name must match `^[a-z0-9-]+$` per `_SKILL_NAME_RE` in `src/agentflow/core/skill_loader.py`).
2. Add `skills/my-skill/SKILL.md` with YAML frontmatter (`name`, `description`) followed by an overview and a "Reference Documents" list, e.g. `skills/business-analysis/SKILL.md`:
   ```markdown
   ---
   name: my-skill
   description: One-line description of when to use this skill.
   ---

   # My Skill

   ## Reference Documents

   - `topic_one.md` — what it covers
   ```
3. Add any reference documents (e.g. `topic_one.md`) as sibling files in the same folder — they are what `topic=` loads via `read_skill`.
4. List the skill's folder name in the agent manifest's `skills:` array (e.g. `manifests/business_analyst_agent.yaml` has `skills: [business-analysis, financial-analysis, equity-research]`).
5. That listing is enough on its own: `Agent._execute` (`src/agentflow/agents/agent.py`) automatically appends `skill_loader.full_content(self.manifest.skills)` — the full SKILL.md plus every reference document, for every listed skill — to the agent's static system prompt on each run. No tool call or extra wiring is required for the agent to receive the guidance.
6. Optionally, reference the skill by name/topic in the `system_prompt` to steer *when* the agent applies which section, e.g. `manifests/business_analyst_agent.yaml` says "Load: read_skill(skill='business-analysis', topic='sustainability_framework')" before each analysis step — this is prose guidance pointing at content that is already embedded, not a required runtime call.
7. `read_skill` (`src/agentflow/tools/skills.py`, backed by `SkillLoader.read()`) is a separate on-demand tool for fetching a specific skill/topic string at call time instead of embedding everything up front. To make it callable you must add `read_skill` to that agent's `tools:` list explicitly — none of the shipped manifests currently do this, so treat step 5 (full-content injection) as the working mechanism and `read_skill` as an available-but-currently-unwired alternative.

## Run the system

From the repo root, after `uv sync` and configuring `.env` (see `README.md`):

```bash
# Start the FastAPI server (loads the registry + engine on startup)
uv run uvicorn agentflow.main:app --reload

# Or via the CLI wrapper
agentflow serve [--reload]

# Submit a task and stream results (server must be running)
agentflow run "<task>" [--context KEY=VALUE ...] [--verbose] [--json]

# Check server health and registered agents
agentflow health
```

## Run tests

```bash
# Full suite
uv run pytest

# Single file
uv run pytest tests/test_registry.py

# Single test, verbose
uv run pytest tests/test_tools.py -v -k test_name
```

`asyncio_mode = "auto"` is set in `pyproject.toml`'s `[tool.pytest.ini_options]`, so `async def test_...` functions run without extra markers.

## Cancel an active run

`POST /api/runs/{run_id}/cancel` — returns `{"status": "cancelled"}` or HTTP 404 if the
run is not active.

Internally `engine.cancel_run(run_id)` calls `task.cancel()` on the asyncio Task stored
in `OrchestratorEngine._run_tasks`. The scheduler catches `asyncio.CancelledError`,
cancels all in-flight subtask tasks, and re-raises; `engine.run()` catches it, emits a
`run:cancelled` SSE event, and runs the normal finally-block cleanup (emitter closed,
context removed). **Cross-replica note:** `_run_tasks` is in-process only — the request
must reach the replica that started the run, or you need sticky sessions.

```bash
curl -X POST http://localhost:8000/api/runs/<run_id>/cancel
```

## Start a follow-up run

`POST /api/runs/{run_id}/followup` with body `{"task": "...", "context": {}, "budget_usd": null}`.
Returns `{"run_id": "<new_run_id>", "status": "started"}`.

The route reads the prior run's `report.md` and `results.jsonl` from disk and injects
them as `prior_run_id`, `prior_task`, `prior_report`, and `prior_results` into
`user_context`. These flow into the planner's prompt just like any `context` fields, so
the new run's planner sees the full prior report and can build on it. A new `run_id` is
generated; stream it normally with `GET /api/runs/<new_run_id>/stream`.

```bash
curl -X POST http://localhost:8000/api/runs/<run_id>/followup \
  -H "Content-Type: application/json" \
  -d '{"task": "Now expand section 3 with more detail"}'
```

## Inject a user message into a running agent

`POST /api/runs/{run_id}/message` with body `{"content": "..."}`.
Returns `{"status": "queued"}`, or HTTP 409 if the run is already finished, or HTTP 404
if the run is not found.

The message is pushed to `RunContext._user_message_queue` (an `asyncio.Queue` in-memory,
a Redis list with `STATE_BACKEND=redis`). `Agent._agentic_loop()` drains one message
after each tool-result batch and appends it as a user turn before the next API call, so
the model sees it without an extra round-trip. A `run:message_received` SSE event is
emitted when the message is consumed. Messages sent during the planning phase sit in the
queue until the first agent loop picks them up.

```bash
curl -X POST http://localhost:8000/api/runs/<run_id>/message \
  -H "Content-Type: application/json" \
  -d '{"content": "Focus only on the European market"}'
```

## Related

- [concepts](concepts.md)
- [conventions](conventions.md)
- [architecture](architecture.md)
- [codebase-map](codebase-map.md)
