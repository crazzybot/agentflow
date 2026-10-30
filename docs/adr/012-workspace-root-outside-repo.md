# ADR-012: Workspace root outside the repository; run-relative artifact paths

**Status:** Accepted
**Date:** 2026-10-30

## Context

The workspace root (`WORKSPACE_DIR`) defaulted to `workspace/` inside the AgentFlow
checkout. By October it held 7.6 GB of agent-built projects, each with its own
`.venv`, `node_modules`, git repo and test suite. That mixed AgentFlow's source with
its output: IDE indexing, grep and code search swept the generated projects, and only
`testpaths` kept pytest from collecting their tests. The root was already configurable,
but `~` was not expanded, and two things assumed the in-repo location. The
`knowledgebase_agent` manifest launched `uv --directory workspace/semantic-kb`, and tests
that did not patch the setting wrote into `./workspace`.

Artifact paths were meant to be relative to the run's own workspace, but they were
stored as the agent typed them. A file written as `/abs/path/to/ws/out.md` or
`./x/../out.md` was recorded in that form in `artifacts.jsonl` and `files_written`.
Absolute records break when the root moves. Mixed spellings make one file look like two
for downstream injection and checkpoint rollback.

## Decision

- `workspace_dir` defaults to **`~/agentflow-workspace`**. `workspace_root()` expands
  `~`. A relative value still resolves against the CWD, for anyone who wants the old
  layout.
- Paths are recorded **relative to the run's workspace** in one canonical form.
  `core.workspace.run_relative()` normalises them for `artifacts.jsonl` and
  `files_written`. A path outside the run's workspace is not recorded.
- Stdio MCP server `args` and `env` values may use the placeholder **`{workspace_root}`**,
  which is replaced with the resolved root. The shipped semantic-kb server uses it.
- The test suite points the root at a temp dir for every test (autouse fixture), so the
  home-directory default is never written by tests.

`.runs/` (`RUNS_DIR`) stays in the repo. It holds AgentFlow's own run records, which
agents cannot reach.

## Consequences

- The repo holds only AgentFlow. Moving or sharing the root is a config change: every
  recorded path (`meta.workspace`, artifacts, `files_written`) is relative, so past runs
  still resolve after the directory moves.
- An existing checkout must move its `workspace/` or set `WORKSPACE_DIR=workspace` to
  keep the old location.
- External tools that point at the old absolute path, such as a Claude Code MCP
  registration for semantic-kb, must be updated by hand.
- Worker hosts and Docker sandboxes need the same root path. That was already true.

## Alternatives considered

- **Keep the in-repo default and only document `WORKSPACE_DIR`.** This leaves the
  mixing as the out-of-the-box behaviour, so it was rejected.
- **`~/.agentflow/workspace` (XDG-style app data).** A hidden directory is awkward for
  deliverables people open directly, so it was rejected.
- **Normalise artifact paths at read time in the API.** Downstream agents and rollback
  read `files_written` long before any API call, so normalising at write time fixes
  every consumer at once.

KB: [architecture.md § Workspaces](../kb/architecture.md#workspaces)
