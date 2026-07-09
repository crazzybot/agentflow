---
title: Concepts & Glossary
last_updated: 2026-07-09
last_verified_sha: dfa1390
sources:
  - src/agentflow/core/models.py
  - src/agentflow/core/registry.py
  - src/agentflow/core/skill_loader.py
  - src/agentflow/core/bus.py
  - src/agentflow/core/context.py
  - src/agentflow/tools/
  - manifests/
status: current
---

# Concepts & Glossary

Domain terms used across AgentFlow, each named as the code names it, with the
module that defines it.

## Agent

`Agent` (`src/agentflow/agents/agent.py`) is the single generic, manifest-driven
runtime shared by every agent type — there is no per-domain subclass. One
`Agent` instance per registered `AgentManifest` runs `_agentic_loop()`, a
tool-calling loop against the shared `LLMClient` until `end_turn`, an
iteration limit, or a budget limit, and returns an `AgentResult`.

## Agent manifest

`AgentManifest` (Pydantic model, `src/agentflow/core/models.py`) declares one
agent's identity and capabilities: `agent_id`, `domain`, `capabilities`,
`tools`, `skills`, `mcp_servers` (`MCPServerConfig`), `system_prompt`, an
optional `decomposition_prompt`, `fallback_for`, `max_concurrency`, and
`max_iterations`. Loaded from YAML/JSON files in `manifests/` (e.g.
`manifests/research_agent.yaml`) by `AgentRegistry`.

## Orchestration

Turning one task into a completed run, split across the `orchestrator/`
package (not read in detail for this doc — see
[architecture](architecture.md)): **plan** (`create_plan()` in
`orchestrator/planner.py` produces an `ExecutionPlan` of `Subtask`s),
**decompose** (`expand_plan()` in `orchestrator/decomposer.py` expands a
subtask further when its manifest has a `decomposition_prompt`), and
**schedule** (`DependencyGraph` in `orchestrator/scheduler.py` exposes
`ready()` subtasks for dispatch as the DAG's dependencies resolve).

## Task / Subtask

`src/agentflow/core/models.py` defines the task-passing shapes: `TaskEnvelope`
(orchestrator → agent: `task_id`, `agent_id`, `instruction`, `context`
(`TaskContext`), `constraints` (`TaskConstraints`)) and `AgentResult` (agent →
orchestrator: `status` — an `AgentStatus` of `success`/`partial`/`failed` —
plus `output`, token/cost fields, and `messages`). A **`Subtask`** is one node
of an LLM-produced **`ExecutionPlan`** (`run_id` + `subtasks: list[Subtask]`),
carrying `agent_id`, `instruction`, `depends_on`, `expected_output`, and an
optional `budget_fraction`.

## Registry

Two independent registries, both named `*Registry`:
- **`AgentRegistry`** (`src/agentflow/core/registry.py`) loads `AgentManifest`s
  from a directory of YAML/JSON files (`load_from_directory()`), and indexes
  them by `agent_id` and by `capabilities` (`by_capability()`), with
  `find_fallback()` looking up a manifest whose `fallback_for` names another
  agent.
- **`ToolRegistry`** (`src/agentflow/tools/registry.py`) holds every
  `ToolDefinition` an agent can call, looked up by name (`get()`,
  `get_many()`) and invoked via `execute()`. The module-level `tool_registry`
  singleton is populated by `tools/builtin.py` and `tools/skills.py` at
  import time.

## Skill

A skill is a directory of reference material (`SKILL.md` plus optional topic
documents) read on demand. `SkillLoader` (`src/agentflow/core/skill_loader.py`)
parses `SKILL.md` frontmatter/body (`frontmatter()`, `description()`, `name()`)
and serves either the whole skill (`read()`) or all of an agent's skills
pre-embedded in its system prompt (`full_content()`). A manifest opts an agent
into a skill via its `skills:` list (empty in
`manifests/research_agent.yaml`); the agent then loads it at runtime through
the `read_skill` tool.

## Tool

`ToolDefinition` (dataclass, `src/agentflow/tools/registry.py`) is the unit an
agent can call: `name`, `description`, `input_schema`, an async `handler`, and
an `impact` (`ToolImpact`: `read_only`, `write`, or `execute`). Manifests list
tool names (e.g. `manifests/research_agent.yaml`'s `tools: [web_search,
fetch_url, wikipedia, arxiv_search, file_read, file_write]`); `ToolRegistry`
resolves those names to `ToolDefinition`s. `read_skill`
(`src/agentflow/tools/skills.py`) is one built-in tool — a thin wrapper that
calls `skill_loader.read()`.

## Message bus

`TaskBus` (`src/agentflow/core/bus.py`) is an in-process asyncio-queue pair
per `run_id` — a dispatch queue (`enqueue_task`/`dequeue_task`) and a result
queue (`publish_result`/`consume_result`) — created via `create_run()` and
torn down via `close_run()`. Its docstring states it is designed to be
swappable for Redis Streams without changing callers; the module-level
`task_bus` singleton exists but per `architecture.md` is not currently on the
request's critical dispatch path.

## Context

`RunContext` (`src/agentflow/core/context.py`) is the per-run shared state:
it stores each subtask's `AgentResult` (`store_result()`/`get_result()`/
`all_results()`), tracks running cost and budget (`total_cost_usd()`,
`remaining_budget_usd()`, `within_budget()`), builds downstream input from
completed dependencies (`build_prior_results()` for text,
`build_prior_messages()` for full conversation replay when there is exactly
one dependency), and arbitrates the human-in-the-loop handshake
(`request_human_input()`/`await_human_input()`/`provide_human_input()`). The
`ContextStore` singleton keys one `RunContext` per `run_id`.

## Related

- [architecture](architecture.md) — how these pieces interact at runtime.
- [codebase-map](codebase-map.md) — where each module lives in the source tree.
