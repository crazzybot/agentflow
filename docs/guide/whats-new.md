---
title: What's new
last_updated: 2026-10-30
last_verified_sha: 564a6a1
sources:
  - docs/adr/
status: current
---

# What's new

Changes to AgentFlow's architecture, newest first, in plain words. Each entry links its
[ADR](../adr/README.md) (why it was built that way) and the KB doc with the details. Bug
fixes and internal refactors are not listed here; see the git history for those.

---

## 30 October 2026: Campaign features are routed to the right agent

- **Each feature gets its own agent.** The campaign's feature planner now sees the agent
  roster, as the run planner does, and assigns each feature to the best-suited agent, for
  example `CodeAgent` for the API and `WriterAgent` for the user guide. Sessions run on
  that feature's agent.
- **`agent_id` on `POST /api/campaigns` is now optional.** Leave it out to get routing
  (the new default). Set it to run every feature on one agent, as before. A feature the
  planner leaves unrouted goes to `CAMPAIGN_DEFAULT_AGENT` (`CodeAgent`).
- **Retry can reassign.** `POST …/features/{fid}/retry` accepts `agent_id` to move a stuck
  feature to another agent. Its checks still don't change.
- Why: [ADR-013](../adr/013-campaign-feature-agent-routing.md). Details:
  [campaigns](../kb/subsystems/campaigns.md).

## 30 October 2026: AgentFlow's data lives outside the repo

- **One data home, `~/agentflow`.** Workspaces (`~/agentflow/workspace`) and run records
  (`~/agentflow/runs`, formerly `.runs/`) no longer sit inside the AgentFlow checkout.
  Set `AGENTFLOW_HOME` to move both at once, or `WORKSPACE_DIR` / `RUNS_DIR` one at a time
  (`~` is expanded). The two must not be inside each other: the server and workers refuse
  to start if they are. To keep the old layout, set `WORKSPACE_DIR=workspace` and
  `RUNS_DIR=.runs`.
- **Artifact paths are always relative to the run's own workspace.** Whatever path an
  agent used, `artifacts.jsonl` and the file lists passed to later subtasks store one
  canonical relative path. That means they keep working if you move the workspace root.
- **MCP servers can refer to the workspace.** A manifest can write `{workspace_root}` in a
  stdio server's `args` or `env`. The bundled semantic-kb server uses it.

[ADR-012](../adr/012-workspace-root-outside-repo.md) ·
[KB: Workspaces](../kb/architecture.md#workspaces)

---

## 30 October 2026: cheaper campaigns

- **A first attempt runs on a cheaper model.** Campaign sessions start on Sonnet at
  medium effort. Only a feature that has failed, regressed or been retried moves up to
  the agent's default model (Opus) at high effort. Configure this with `CAMPAIGN_SESSION_*` /
  `CAMPAIGN_ESCALATION_*`.
- **Sessions have a default budget** of $3, and a session that reaches it stops instead
  of waiting for a human. Sessions also get more turns (40) and a list of the
  workspace's files, so they spend less time looking around.
- **Continuations reuse the prompt cache.** The agent prompt now shows only the date,
  not the time, so a continued conversation is no longer re-written to the cache.
- **Small edits are cheap.** `file_write` has a `replace_text` mode that replaces one
  exact snippet, so the agent no longer has to rewrite a whole file.
- **Interrupted work is charged.** Spend from an agent call that a restart cut off now
  counts toward run and campaign costs.
  [ADR-011](../adr/011-campaign-session-cost-controls.md) · [KB](../kb/subsystems/campaigns.md)

## 30 October 2026: campaigns can be unstuck

- **A feature's sessions are tracked on the feature itself,** including the one in
  progress. A campaign view can show each feature with its full session history and a
  live "in progress" marker.
- **A campaign stopped at a limit can continue.** Change `max_sessions` or the budgets
  (`PATCH /api/campaigns/{id}`), then resume. If something still blocks it, resume says
  exactly what, and how to fix each part.
- **A blocked feature can be retried** (`POST /api/campaigns/{id}/features/{fid}/retry`),
  optionally with written guidance for its next session. Its checks stay fixed, and only
  the verifier can still mark it passing.
  [ADR-008](../adr/008-campaign-ledger-in-code.md) (amended) · [KB](../kb/subsystems/campaigns.md)

## October 2026: the Managed Agents redesign

Eight changes, made one after another, turned AgentFlow from "one process that runs a
plan once" into a system that keeps a durable record of every run, can resume it, checks
its own work, and can move execution elsewhere.

- **Agents can run on worker processes.** With `DISPATCH_MODE=workers`, the server keeps
  planning and checking, while `agentflow worker` processes, possibly on other machines,
  run the agents. A worker that dies just causes a retry.
  [ADR-010](../adr/010-remote-agent-workers.md) · [KB](../kb/subsystems/workers.md)
- **Agent commands can run in a Docker sandbox,** and manifests can restrict shell
  commands. `SANDBOX_BACKEND=docker` gives each run a locked-down container (it fails
  rather than falling back to the host), and `tool_policies` adds per-agent command
  allowlists. A timed-out command is now actually killed.
  [ADR-009](../adr/009-sandbox-and-tool-policies.md) · [KB](../kb/subsystems/sandbox-and-policies.md)
- **Campaigns build big projects across many sessions.** A fixed feature list is
  implemented one feature per fresh-context session. A feature counts as done only when
  the independent verifier confirms it, and passing features are re-checked for
  regressions. [ADR-008](../adr/008-campaign-ledger-in-code.md) · [KB](../kb/subsystems/campaigns.md)
- **Subtasks are checked independently, and the workspace is checkpointed.** The planner
  can give subtasks acceptance criteria that a read-only verifier checks, with repair
  rounds when they fail. Every finished subtask is snapshotted into a shadow git repo,
  and a failed attempt's files are rolled back before the retry.
  [ADR-006](../adr/006-independent-verification.md) · [ADR-007](../adr/007-shadow-repo-checkpoints.md) · [KB](../kb/subsystems/verification-and-checkpoints.md)
- **Long conversations are shortened for the model, without deleting anything.** Past
  about 100k tokens, older tool results are replaced by stubs the agent can recall. The
  full history is kept, and the prompt cache keeps working.
  [ADR-005](../adr/005-read-time-context-view.md) · [KB](../kb/subsystems/context-view.md)
- **Interrupted runs can be resumed,** and replicas no longer step on each other. A
  restart marks runs interrupted instead of dead, and `POST /runs/{id}/resume` continues
  them from where they stopped. With Redis, leases keep replicas from interfering, and
  cancel works from any replica.
  [ADR-004](../adr/004-resume-from-log-with-leases.md) · [KB](../kb/subsystems/resume-and-leases.md)
- **Every run keeps a durable session log.** `.runs/<id>/session.jsonl` records
  everything, including every agent conversation, and is what resume, campaigns and
  audits read back. [ADR-003](../adr/003-file-backed-session-log.md) · [KB](../kb/subsystems/session-log.md)
- **Each run gets its own workspace.** Runs no longer share one directory. A run gets a
  fresh workspace unless it names a project workspace, and runs on the same project take
  turns. [ADR-002](../adr/002-per-run-and-named-workspaces.md) · [KB](../kb/architecture.md#workspaces)

The docs were reorganised at the same time: this guide was added, the July design doc,
gap analysis and presentations moved to [`docs/archive/`](../archive/README.md), and ADRs
are written for every major decision.

## July 2026: Redis state backend

`STATE_BACKEND=redis` lets several API replicas share live runs, so streams and human
input work from any replica. [ADR-001](../adr/001-redis-state-backend.md) ·
[KB](../kb/subsystems/redis-backend.md)
