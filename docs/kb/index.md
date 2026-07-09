---
title: AgentFlow Knowledge Base — Index
last_updated: 2026-07-09
last_verified_sha: 1d91fe3
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
> (`design.md`, `../agentflow_gap_analysis.md`, presentation material). Do not
> duplicate those; link to them only when needed.

## Reading order

1. [Architecture](architecture.md) — how AgentFlow works end-to-end.
2. [Codebase map](codebase-map.md) — where everything lives.
3. [Concepts](concepts.md) — domain glossary.
4. [Conventions](conventions.md) — patterns, structure, testing.
5. [How-to](how-to.md) — task recipes.

## Subsystem deep-dives

`subsystems/` holds per-subsystem docs added as the system grows. (Empty today.)

## Keeping this KB current

After any implementation/improvement task, run the `update-kb` skill (see
`CLAUDE.md`). Each doc's frontmatter carries `last_verified_sha` + `sources`;
drift is detected by `git log <last_verified_sha>..HEAD -- <sources>`.
