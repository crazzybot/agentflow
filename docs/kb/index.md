---
title: AgentFlow Knowledge Base — Index
last_updated: 2026-07-15
last_verified_sha: 17a27d3
sources:
  - src/agentflow/
  - manifests/
  - skills/
status: current
---

# AgentFlow Knowledge Base

Agent-facing knowledge base. **Read this first on any non-trivial task**, then
the doc(s) relevant to your change, before exploring source.

> These docs are maintained separately from the human/design docs in `docs/`
> (`design.md`, `agentflow_gap_analysis.md`, presentation material). Do not
> duplicate those; link to them only when needed.

## Reading order

1. [Architecture](architecture.md) — how AgentFlow works end-to-end; request lifecycle and component map.
2. [Conventions](conventions.md) — patterns, code style, async rules, testing, anti-patterns.

## Subsystem deep-dives

`subsystems/` holds per-subsystem docs added as the system grows.

- [subsystems/redis-backend](subsystems/redis-backend.md) — the optional Redis state
  backend: bus, context store, and SSE registry variants selected by
  `STATE_BACKEND=redis`, plus cross-replica HITL and streaming.

## On-demand skills (invoke instead of reading)

These are available as `/skill-name` — they inject live context when needed rather
than being loaded every session:

- `/how-to` — step-by-step recipes: add an agent, add a tool, attach a skill, cancel/followup/message API
- `/update-kb` — reconcile `docs/kb/` after an implementation task
- `/session-wrap` — end-of-session KB review and session log entry

## Related resources

- [`docs/adr/`](../adr/README.md) — Architecture Decision Records (rationale for major decisions)
- [`docs/session-log.md`](../session-log.md) — breadcrumb trail of per-session decisions

## Keeping this KB current

After any implementation/improvement task, run the `update-kb` skill (see
`CLAUDE.md`). Each doc's frontmatter carries `last_verified_sha` + `sources`;
drift is detected by `git log <last_verified_sha>..HEAD -- <sources>`.
