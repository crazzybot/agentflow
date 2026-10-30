---
title: Concepts
last_updated: 2026-10-30
last_verified_sha: 629fee4
sources:
  - src/agentflow/core/models.py
  - src/agentflow/core/workspace.py
  - src/agentflow/core/session.py
  - src/agentflow/orchestrator/
  - src/agentflow/agents/
  - src/agentflow/tools/sandbox.py
  - src/agentflow/tools/policy.py
status: current
---

# Concepts

The vocabulary used across AgentFlow, in roughly the order you meet it.

### Run
One execution of one task, identified by a `run_id`. A run is planned, executes its
subtasks, writes a report, and ends as **complete**, **error** or **cancelled**. A
crash can also leave it **interrupted**, in which case it can be resumed.

### Plan and subtask
The **plan** is a dependency graph (DAG) of **subtasks**. Each subtask has an
instruction, the agent that should do it, the subtasks it depends on, and optionally
a budget share, a model-tier hint and **acceptance criteria**. A subtask starts when its
dependencies finish and receives their results as context. Independent subtasks run in
parallel.

### Planner and decomposer
The **planner** is an agent with read-only tools. It looks at the workspace and writes
the plan. The **decomposer** optionally splits one big subtask into **micro-steps** at
the moment the subtask is dispatched, so it sees the workspace as upstream subtasks
left it. A code subtask that needs eight files, for example, becomes several parallel
steps plus an aggregator.

### Agent and manifest
Every agent is the same generic loop, configured by a **manifest**
(`manifests/*.yaml`). The manifest sets the system prompt, allowed tools, skills (reference
docs injected into the prompt), MCP servers, model tier, iteration limit, tool limits
and **tool policies**. One **invocation** of an agent is one conversation with Claude:
it calls tools and gets results until it produces a final answer.

### Tools
The functions agents call. The built-in ones are `file_read`, `file_write`, `bash_exec`,
`bash_exec_readonly`, `python_exec`, `web_search`, `fetch_url`, `download_document`,
`arxiv_search`, `wikipedia` and `read_skill`, plus any tools from the agent's MCP servers.
Tools that **execute code** run in the **sandbox**. Tools that use **credentials** (web
search, MCP) run in the AgentFlow process, so agent-run code never sees a secret.

### Tool policies
Checks that run **before** every tool call and can refuse it. For example, a
command allowlist restricts which shell commands `bash_exec` may run, and per-tool call
limits cap how often a tool can be used. A refused call never runs; the agent is told why.

### Sandbox
**Where agent-issued commands run.** `local` is a subprocess on the host with credentials
stripped from its environment. `docker` is a per-run, locked-down container that shares
only the workspace. If Docker is unavailable, the command fails rather than running on
the host. [ADR-009](../adr/009-sandbox-and-tool-policies.md)

### Workspace
The directory a run's tools work in. By default each run gets a fresh one
(`workspace/runs/<run_id>/`). A **named workspace** (`workspace/<name>/`) is a long-lived
project that several runs work on, one at a time. Follow-up runs continue in their
parent's workspace. [ADR-002](../adr/002-per-run-and-named-workspaces.md)

### Session log
The durable, append-only record of a run (`.runs/<run_id>/session.jsonl`). It holds
inputs, the plan, every subtask outcome, cost, human input, and **every agent
invocation's full conversation**. It is the source of truth that resume, campaigns and
audits read back. [ADR-003](../adr/003-file-backed-session-log.md)

### SSE stream
The live event feed of a run (`GET /api/runs/{id}/stream`), for UIs and the CLI: plan
created, subtask dispatched, tool calls, thinking, verification, completion. It's a
*view* for watching. The session log is the record.

### Context view
What an agent actually sends to Claude each turn. Once a conversation gets long, older
bulky tool results are replaced by short stubs that the agent can expand again with a
`recall_tool_result` tool. The full history is never edited.
[ADR-005](../adr/005-read-time-context-view.md)

### Verification and repair
If a subtask has acceptance criteria, a separate **read-only verifier** checks them
against the workspace when the agent says it's done. If a criterion fails, the agent gets
the findings and continues its own conversation to fix them (**repair**). If it still
fails, the result is marked **partial** and the report says what's missing.
[ADR-006](../adr/006-independent-verification.md)

### Checkpoint
A git snapshot of the workspace, taken at the start of the run and after each finished
subtask. Snapshots go into a separate *shadow* repository under the run's directory,
never into a project's own `.git`. If an attempt fails, the files it wrote are rolled
back before the retry. [ADR-007](../adr/007-shadow-repo-checkpoints.md)

### Resume
Restarting an **interrupted** run from its session log: the plan is reused, finished
subtasks are skipped, and subtasks that were mid-flight continue from their last logged
turn. [ADR-004](../adr/004-resume-from-log-with-leases.md)

### Lease
With several API replicas, a short-lived Redis key that says which process owns a run
(or a campaign). It prevents two replicas from resuming the same run, stops a restarting
replica from declaring another replica's live runs dead, and lets you cancel a run from
any replica.

### Campaign
Long-horizon work: a **feature ledger** is planned once, then built over many **sessions**.
Each session is an ordinary run that implements one feature with a fresh context, and a
feature counts as done only when the verifier confirms it. Agents can't edit the ledger.
An operator can **retry** a feature that keeps failing, with guidance for its next
session, and can change the session and budget limits. A feature's first session runs
on a cheaper model, and only a feature that has failed moves up to a stronger one.
[ADR-008](../adr/008-campaign-ledger-in-code.md),
[ADR-011](../adr/011-campaign-session-cost-controls.md)

### Worker
A separate process (`agentflow worker`) that executes agent invocations queued by the API
server. Planning, scheduling, verification and the session log stay on the server.
[ADR-010](../adr/010-remote-agent-workers.md)

### Human in the loop (HITL)
The points where a person can step in: approving a budget increase or more iterations,
messaging a running agent, cancelling, resuming, or pausing a campaign.
