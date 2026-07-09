---
title: How-To Recipes
last_updated: 2026-07-09
last_verified_sha: ec54170
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
5. In the manifest's `system_prompt`, instruct the agent to call the `read_skill` tool, e.g. `read_skill(skill='business-analysis')` for the SKILL.md overview, or `read_skill(skill='business-analysis', topic='sustainability_framework')` for a named reference document (`src/agentflow/tools/skills.py` registers `read_skill`; resolution logic is in `SkillLoader.read()`).
6. `read_skill` is a global tool (registered once) available to any agent — declaring `skills:` in the manifest only advertises which skills exist to the planner/agent; the agent still needs `read_skill` in its `tools:` list to call it (all shipped manifests include `read_skill` alongside their skills).

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

## Related

- [concepts](concepts.md)
- [conventions](conventions.md)
- [architecture](architecture.md)
- [codebase-map](codebase-map.md)
