# Architecture Decision Records

Short records of significant architectural choices: what was decided, why, and which
alternatives were rejected.

## ADRs and the KB

| | ADRs (`docs/adr/`) | KB (`docs/kb/`) |
| --- | --- | --- |
| Answers | **Why** is it this way? What else was considered? | **How** does it work now? |
| Changes | **Immutable once accepted.** Amend or supersede; never rewrite. | Kept current on every change (`update-kb`). |
| Linked from | KB docs, at the point where the decision shows up | ADRs, for the current details |

Don't copy an ADR's rationale into the KB. Link to it. Before changing behaviour that an
ADR covers, read the ADR. If you are reversing the decision, write a new ADR that
supersedes it.

## When to write one

Write an ADR when a change does any of these:

- chooses between real alternatives, and the losing options would still be plausible to
  someone later;
- creates a constraint that is expensive to undo: a storage format, a protocol, a
  security boundary, or a default that changes behaviour;
- deliberately *doesn't* build something obvious (e.g. ADR-005 defers auto-compact).

Don't write one for local refactors, bug fixes, or choices with a single obvious answer.
The `update-kb` skill asks this question at the end of each task.

## Changing an accepted ADR

- **The decision still holds, but facts moved on.** Append a dated
  `## Later developments (YYYY-MM-DD)` section and set the status to
  `Accepted (amended YYYY-MM-DD)`. ADR-001 is an example.
- **The decision is reversed or replaced.** Write a new ADR, set the old one's status to
  `Superseded by ADR-NNN`, and add one line pointing to it. Leave its body alone.

## Format

`NNN-slug.md` (zero-padded, hyphen-separated):

```markdown
# ADR-NNN: Title

**Status:** Proposed | Accepted | Accepted (amended YYYY-MM-DD) | Superseded by ADR-XXX
**Date:** YYYY-MM-DD

## Context
Why did this decision need to be made?

## Decision
What was decided?

## Consequences
What changes as a result? What becomes easier or harder? Known limits.

## Alternatives considered
What else was on the table and why was it rejected?

KB: [link to the doc describing the current implementation]
```

## Index

| ADR | Title | Status |
|-----|-------|--------|
| [001](001-redis-state-backend.md) | Redis state backend for multi-replica runs | Accepted (amended 2026-10-29) |
| [002](002-per-run-and-named-workspaces.md) | Per-run and named workspaces | Accepted |
| [003](003-file-backed-session-log.md) | A file-backed, append-only session log is the run's source of truth | Accepted |
| [004](004-resume-from-log-with-leases.md) | Resume interrupted runs from the log; per-run ownership leases | Accepted |
| [005](005-read-time-context-view.md) | Bound context with a read-time view, not history mutation; defer auto-compact | Accepted |
| [006](006-independent-verification.md) | Independent read-only verification of acceptance criteria | Accepted |
| [007](007-shadow-repo-checkpoints.md) | Workspace checkpoints in a shadow git repository | Accepted |
| [008](008-campaign-ledger-in-code.md) | Campaigns with a code-enforced feature ledger | Accepted (amended 2026-10-30) |
| [009](009-sandbox-and-tool-policies.md) | Sandbox interface for exec tools (Docker fails closed); pre-tool-use policy chain | Accepted |
| [010](010-remote-agent-workers.md) | Remote agent execution through a dedicated work queue | Accepted |
| [011](011-campaign-session-cost-controls.md) | Campaign session cost controls (cheap first attempt, escalation, cache-stable prompt) | Accepted |
| [012](012-workspace-root-outside-repo.md) | AgentFlow data home outside the repository; run-relative artifact paths | Accepted |
