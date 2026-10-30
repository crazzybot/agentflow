---
title: What's new
last_updated: 2026-10-29
last_verified_sha: b513b90
sources:
  - docs/adr/
status: current
---

# What's new

Changes to AgentFlow's architecture, newest first, in plain words. Each entry links its
[ADR](../adr/README.md) (why it was built that way) and the KB doc with the details. Bug
fixes and internal refactors are not listed here; see the git history for those.

---

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
