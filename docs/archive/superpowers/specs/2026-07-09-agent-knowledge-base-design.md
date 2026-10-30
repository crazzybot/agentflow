# AgentFlow Agent Knowledge Base — Design

- **Date:** 2026-07-09
- **Status:** Approved (design)
- **Author:** brainstormed with Claude Code

## Problem

Coding agents (e.g. Claude Code) rediscover how AgentFlow works from scratch on
every new task. This wastes effort and produces inconsistent mental models. We
want a collection of always-up-to-date, interlinked markdown documents that give
an agent fast, reliable project knowledge, plus a working rule that requires the
agent to keep those documents current after each implementation/improvement task.

## Decisions (locked)

1. **Relationship to existing docs — fully separate.** The existing long-form
   docs (`docs/design.md`, `README.md`, `docs/agentflow_gap_analysis.md`,
   presentation material) stay untouched as human/design artifacts. The KB is a
   parallel, agent-facing tree built from scratch and does not depend on them.
2. **Enforcement — rules + self-review skill.** No hard hooks. `CLAUDE.md` rules
   plus a dedicated `update-kb` skill (a checklist the agent runs at end of task).
3. **Granularity — layered: core set now, expandable.** A small core doc set
   now; a `subsystems/` directory as the growth valve for per-subsystem deep
   dives added later.
4. **Freshness — metadata frontmatter + source anchors.** Each doc carries
   `last_updated`, `last_verified_sha`, and a `sources` list of the code paths it
   describes. Drift is detected mechanically by diffing those paths since the
   recorded SHA.

## Architecture

### Location & structure

```
docs/kb/
  index.md            # hub: map of the KB + recommended reading order
  architecture.md     # how AgentFlow works end-to-end (orchestrator -> agents -> llm/tools)
  codebase-map.md     # where things live across src/agentflow/*, manifests/, skills/
  concepts.md         # glossary / domain model: agent, manifest, orchestration, skill, tool
  conventions.md      # coding patterns, structure rules, testing approach
  how-to.md           # task recipes: add an agent, add a tool, wire a manifest, run tests
  subsystems/         # empty initially; per-subsystem deep dives added as the system grows
```

`CLAUDE.md` (new, at repo root) is the discovery entry point. It points agents at
`docs/kb/index.md` and carries the standing maintenance rules (below).

### Document format

Every KB doc opens with YAML frontmatter:

```yaml
---
title: Architecture Overview
last_updated: 2026-07-09
last_verified_sha: 073eed6        # HEAD when the doc was last reconciled against code
sources:                          # code paths this doc describes (relative to repo root)
  - src/agentflow/orchestrator/
  - src/agentflow/core/
status: current                   # current | stale | draft
---
```

Body conventions:

- Interlink docs with **relative markdown links**: `[concepts](concepts.md)`.
- Anchor into code with relative links: `[orchestrator](../../src/agentflow/orchestrator/)`.
- Keep prose lean and agent-optimized: facts, invariants, and pointers over prose.
- The `sources` list is authoritative for drift detection — keep it accurate.

### Drift detection

Staleness of a doc is a mechanical query, not a judgment call:

```
git log --oneline <last_verified_sha>..HEAD -- <each source path>
```

Non-empty output means the code the doc describes changed since it was last
verified, so the doc is potentially stale. When reconciled, bump
`last_verified_sha` to current HEAD and refresh `last_updated`.

### Enforcement

**`CLAUDE.md` standing rules:**

- *Start of any non-trivial task:* read `docs/kb/index.md` and the relevant KB
  doc(s) before exploring code.
- *Before declaring a task complete:* run the `update-kb` skill.

**`.claude/skills/update-kb/` — project skill checklist:**

1. Determine which `sources` paths the change touched.
2. For each KB doc whose `sources` overlap, reconcile the prose with the new
   reality.
3. Update `last_updated` and bump `last_verified_sha` to current HEAD on
   reconciled docs.
4. If a change introduces a whole new subsystem worth its own doc, add one under
   `subsystems/` and link it from `index.md`.
5. Run the drift query across all docs; flag/fix any that show as stale (or set
   `status: stale` if it cannot be fixed in the current task).

## Rationale

- **Separate tree** keeps design/presentation docs pristine while giving agents a
  lean, purpose-built surface.
- **Core-set-now** avoids over-documenting a moving system; `subsystems/` is the
  growth valve.
- **SHA + sources** makes drift detectable rather than hoped-for, even without a
  hard hook gate.
- **Skill over hook** keeps the mechanism lightweight and portable.

## Known limitation

Because enforcement is a skill (not a hook), the guarantee is only as strong as
the agent honoring `CLAUDE.md`. The `last_verified_sha` check is the safety net:
even a forgetful agent leaves an auditable stale-marker behind that the next
`update-kb` run will surface.

## Out of scope

- Hard hook/pre-commit gating (explicitly not chosen).
- Rewriting or migrating existing long-form docs into the KB.
- Automated CI enforcement of freshness.

## Success criteria

- A new agent can orient on AgentFlow from `docs/kb/index.md` without reading
  source first.
- Each core doc exists with accurate frontmatter and source anchors seeded from
  the current codebase (HEAD).
- `CLAUDE.md` and the `update-kb` skill exist and are internally consistent.
- The drift query works against real `sources`/`last_verified_sha` values.
