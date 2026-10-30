---
title: AgentFlow Knowledge Base — Index
last_updated: 2026-10-30
last_verified_sha: 74de1b7
sources:
  - src/agentflow/
  - manifests/
  - skills/
status: current
---

# AgentFlow Knowledge Base

Agent-facing knowledge base. **Read this first on any non-trivial task**, then
the doc(s) relevant to your change, before exploring source.

> The KB describes how the system works **now**, for agents. People should start with the
> [architecture guide](../guide/README.md). The reasons behind major choices
> are in [`docs/adr/`](../adr/README.md) (immutable decision records). Historical
> material (the July design doc, gap analysis, presentations, completed plans) is
> in [`docs/archive/`](../archive/README.md). It is not maintained, so don't rely on it.

## Reading order

1. [Architecture](architecture.md) — how AgentFlow works end-to-end; request lifecycle and component map.
2. [Conventions](conventions.md) — patterns, code style, async rules, testing, anti-patterns.

## Subsystem deep-dives

`subsystems/` holds per-subsystem docs added as the system grows.

- [subsystems/redis-backend](subsystems/redis-backend.md) — the optional Redis state
  backend: bus, context store, and SSE registry variants selected by
  `STATE_BACKEND=redis`, plus cross-replica HITL and streaming.
- [subsystems/session-log](subsystems/session-log.md) — the always-on, append-only
  `session.jsonl` per run (inputs, plan, results, cost, HITL, full agent transcripts),
  its slice API and `replay()` into a `RunSnapshot`.
- [subsystems/context-view](subsystems/context-view.md) — the read-time view of an
  agent's history sent to the model: older tool results stubbed past a token trigger
  (cache-stable, history untouched), recall tool, `peak_prompt_tokens` measurement.
- [subsystems/verification-and-checkpoints](subsystems/verification-and-checkpoints.md) —
  acceptance criteria checked by an independent read-only verifier (with repair rounds),
  and per-subtask git snapshots of the workspace in a shadow repo with rollback of
  failed attempts.
- [subsystems/sandbox-and-policies](subsystems/sandbox-and-policies.md) — where exec
  tools run (local or a locked-down per-run Docker container, fail-closed) and the
  pre-tool-use policy chain (command allowlists, call limits).
- [subsystems/workers](subsystems/workers.md) — `DISPATCH_MODE=workers`: subtask agents
  run in `agentflow worker` processes over Redis (claim/heartbeat, failures as failed
  attempts, cancel), with the server as the session log's only writer.
- [subsystems/campaigns](subsystems/campaigns.md) — long-horizon work as fresh-context
  sessions over a code-enforced feature ledger; a feature passes only on independent
  verification, with regression checks, bounds, and pause/resume across restarts.
- [subsystems/resume-and-leases](subsystems/resume-and-leases.md) — resuming
  interrupted runs from their session log (`wake()`, `POST /runs/{id}/resume`,
  reconciliation) and the Redis per-run leases behind multi-replica resume and cancel.

## On-demand skills (invoke instead of reading)

These are available as `/skill-name` — they inject live context when needed rather
than being loaded every session:

- `/how-to` — step-by-step recipes: add an agent, add a tool, attach a skill, cancel/resume/followup/message API, campaigns, named workspaces
- `/update-kb` — reconcile `docs/kb/` after an implementation task
- `/session-wrap` — end-of-session KB review and session log entry

## Related resources

- [`docs/adr/`](../adr/README.md) — Architecture Decision Records (rationale for major decisions)
- [`docs/session-log.md`](../session-log.md) — breadcrumb trail of per-session decisions

## Keeping this KB current

After any implementation/improvement task, run the `update-kb` skill (see
`CLAUDE.md`). Each doc's frontmatter carries `last_verified_sha` + `sources`;
drift is detected by `git diff --stat <last_verified_sha> HEAD -- <sources>` (a tree
comparison — unlike `git log <sha>..HEAD` it still works when the recorded SHA is a
PR-branch commit that was squash-merged and so is not an ancestor of `main`).
