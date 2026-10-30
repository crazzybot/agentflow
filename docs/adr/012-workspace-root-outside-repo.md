# ADR-012: AgentFlow data home outside the repository; run-relative artifact paths

**Status:** Accepted
**Date:** 2026-10-30

## Context

Both data directories defaulted to locations inside the AgentFlow checkout: the
workspace root (`WORKSPACE_DIR`, `workspace/`) and the run records (`RUNS_DIR`, `.runs/`).
The workspace By October it held 7.6 GB of agent-built projects, each with its own
`.venv`, `node_modules`, git repo and test suite. That mixed AgentFlow's source with
its output: IDE indexing, grep and code search swept the generated projects, and only
`testpaths` kept pytest from collecting their tests. The root was already configurable,
but `~` was not expanded, and two things assumed the in-repo location. `runs_dir` was read raw at about a dozen call sites, so it could not use `~` either. The
`knowledgebase_agent` manifest launched `uv --directory workspace/semantic-kb`, and tests
that did not patch the setting wrote into `./workspace`.

Artifact paths were meant to be relative to the run's own workspace, but they were
stored as the agent typed them. A file written as `/abs/path/to/ws/out.md` or
`./x/../out.md` was recorded in that form in `artifacts.jsonl` and `files_written`.
Absolute records break when the root moves. Mixed spellings make one file look like two
for downstream injection and checkpoint rollback.

## Decision

- A single data home, **`AGENTFLOW_HOME`** (default **`~/agentflow`**), holds two
  sibling directories: `workspace/` (the `WORKSPACE_DIR` default) and `runs/` (the
  `RUNS_DIR` default). Either can still be set explicitly. `workspace_root()` and
  `runs_root()` expand `~` and resolve; every runs-dir consumer goes through
  `runs_root()`. A relative value still resolves against the CWD, for anyone who wants
  the old layout.
- The two directories **must not contain each other**. `check_data_dirs()` enforces this
  at API and worker startup. Runs recorded before per-run workspaces work in the
  workspace root itself, so a runs dir inside it would expose session logs and campaign
  ledgers (ADR-008) to agent tools.
- Paths are recorded **relative to the run's workspace** in one canonical form.
  `core.workspace.run_relative()` normalises them for `artifacts.jsonl` and
  `files_written`. A path outside the run's workspace is not recorded.
- Stdio MCP server `args` and `env` values may use the placeholder **`{workspace_root}`**,
  which is replaced with the resolved root. The shipped semantic-kb server uses it.
- The test suite points both directories at a temp home for every test (autouse
  fixture), so the home-directory defaults are never written by tests.

## Consequences

- The repo holds only AgentFlow. Backing up, moving or sharing AgentFlow's data is one
  directory, and one volume for replicas and workers, which must share both. Every
  recorded path (`meta.workspace`, artifacts, `files_written`) is relative, and
  checkpoints get their work tree on each git call, so past runs still resolve after a
  move.
- An existing checkout must move `workspace/` and `.runs/` into the home, or set
  `WORKSPACE_DIR=workspace` and `RUNS_DIR=.runs` to keep the old layout.
- External tools that point at the old absolute path, such as a Claude Code MCP
  registration for semantic-kb, must be updated by hand.
- Worker hosts and Docker sandboxes need the same root path. That was already true.

## Alternatives considered

- **Keep the in-repo default and only document `WORKSPACE_DIR`.** This leaves the
  mixing as the out-of-the-box behaviour, so it was rejected.
- **Separate top-level folders (`~/agentflow-workspace`, `~/agentflow-runs`).** This is
  the smallest change, but it splits one system's data across two places to back up and
  mount. Rejected.
- **Runs inside the workspace root (`<root>/.runs`).** This exposes run records to
  legacy root-level runs, as described above. Rejected.
- **`~/.agentflow` (XDG-style app data).** A hidden directory is awkward for deliverables
  people open directly, so it was rejected.
- **Normalise artifact paths at read time in the API.** Downstream agents and rollback
  read `files_written` long before any API call, so normalising at write time fixes
  every consumer at once.

KB: [architecture.md § Workspaces](../kb/architecture.md#workspaces)
