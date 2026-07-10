---
name: how-to
description: Step-by-step recipes for common AgentFlow extension tasks: adding a new
             agent, adding a new tool, attaching a skill to an agent, and using the
             cancel/followup/message API endpoints. Invoke when the user asks how to
             add or wire up any of these things.
invocation: manual
---

# AgentFlow How-To Recipes

## Add a new agent

1. Create `manifests/my_agent.yaml` (`.json`/`.yml` also accepted — `AgentRegistry.load_from_directory` globs all three).
2. Required fields (see `AgentManifest` in `src/agentflow/core/models.py`):
   - `agent_id` — unique string; used as the registry dict key
   - `domain` — string
   - `system_prompt` — string
3. Optional fields (defaults shown):
   - `version` (default `"1.0.0"`)
   - `capabilities` (list, default `[]`) — shown to the LLM planner via `AgentRegistry.summary()`
   - `tools` (list, default `[]`) — allow-list of tool names already in `tool_registry`
   - `skills` (list, default `[]`) — skill folder names under `skills/`
   - `mcp_servers` (list of `MCPServerConfig`, default `[]`)
   - `decomposition_prompt` (str, optional) — used by `orchestrator/decomposer.py` to split large subtasks
   - `fallback_for` (list, default `[]`) — agent_ids this agent substitutes for on failure
   - `max_concurrency` (int, default `3`)
   - `max_iterations` (int or `None`, default `None` — falls back to `settings.agent_max_iterations`)
4. No code change needed. `src/agentflow/main.py` calls `registry.load_from_directory(settings.manifests_dir)` at startup; the new manifest is picked up on the next server start.
5. A manifest that fails Pydantic validation, or whose filename stem is already loaded, is skipped with a logged warning — the process still starts with the remaining agents.

## Add a new tool

1. Implement an async handler in `src/agentflow/tools/builtin.py` (or a new module under `src/agentflow/tools/`):
   ```python
   async def _my_tool(**named_params) -> str:
       ...  # return a string — this is what the agent sees as the tool result
   ```
2. Register it at module import time:
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
3. If you added a new module (not `builtin.py`/`skills.py`), import it in `src/agentflow/tools/__init__.py` alongside the existing imports — registration only happens when the module is imported.
4. Add the tool's `name` to the `tools:` list of every agent manifest that should use it. `Agent` calls `tool_registry.get_many(self.manifest.tools)` to build its per-agent tool set; a registered-but-unlisted tool is invisible to that agent.
5. `ToolRegistry.execute()` catches handler exceptions and returns an error string to the agent — no extra error handling needed in the handler.

## Attach a skill to an agent

1. Create `skills/my-skill/` (name must match `^[a-z0-9-]+$` per `_SKILL_NAME_RE` in `src/agentflow/core/skill_loader.py`).
2. Add `skills/my-skill/SKILL.md` with YAML frontmatter (`name`, `description`) and a body:
   ```markdown
   ---
   name: my-skill
   description: One-line description of when to use this skill.
   ---

   # My Skill

   ## Reference Documents

   - `topic_one.md` — what it covers
   ```
3. Add any reference documents (e.g. `topic_one.md`) as siblings in the same folder.
4. List the skill folder name in the agent manifest's `skills:` array.
5. That's all: `Agent._execute` automatically appends `skill_loader.full_content(self.manifest.skills)` — the full SKILL.md plus every reference document — to the agent's system prompt. No tool call needed.
6. `read_skill` (`src/agentflow/tools/skills.py`) is an alternative on-demand tool for fetching a specific skill/topic at call time instead of embedding everything. Add `read_skill` to the manifest's `tools:` list to make it callable; none of the shipped manifests currently use it, so treat step 5 as the working mechanism.

## Cancel an active run

```bash
curl -X POST http://localhost:8000/api/runs/<run_id>/cancel
# returns {"status": "cancelled"} or 404 if not active
```

`engine.cancel_run(run_id)` cancels the asyncio Task in `OrchestratorEngine._run_tasks`.
The scheduler catches `CancelledError`, cancels all in-flight subtask tasks, and re-raises;
`engine.run()` emits `run:cancelled` and cleans up.

**Cross-replica note:** `_run_tasks` is in-process only — the cancel request must reach
the replica that started the run, or use sticky sessions. With `STATE_BACKEND=redis` this
limitation still applies (no distributed task cancellation yet).

## Start a follow-up run

```bash
curl -X POST http://localhost:8000/api/runs/<run_id>/followup \
  -H "Content-Type: application/json" \
  -d '{"task": "Now expand section 3 with more detail"}'
# returns {"run_id": "<new_run_id>", "status": "started"}
```

The route reads the prior run's `report.md` and `results.jsonl` from disk and injects
them as `prior_run_id`, `prior_task`, `prior_report`, and `prior_results` into
`user_context`. The new planner sees the full prior report and can build on it.

## Inject a message into a running agent

```bash
curl -X POST http://localhost:8000/api/runs/<run_id>/message \
  -H "Content-Type: application/json" \
  -d '{"content": "Focus only on the European market"}'
# returns {"status": "queued"}, 409 if run is finished, 404 if not found
```

The message is pushed to `RunContext._user_message_queue`. `Agent._agentic_loop()` drains
one message after each tool-result batch and appends it as a user turn before the next
LLM call. A `run:message_received` SSE event is emitted when it is consumed.
