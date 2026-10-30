# ADR-008: Campaigns with a code-enforced feature ledger

**Status:** Accepted (amended 2026-10-30: see "Later developments")
**Date:** 2026-10-29

## Context

Long-horizon work needs many fresh-context sessions held together by durable progress
state. The reference harness pattern (an initializer plus coding sessions over
`feature_list.json`) relies on prompt instructions such as "it is catastrophic to edit
features", and lets the agent mark its own features passing.

## Decision

- **The ledger lives outside every agent workspace** (`<runs_dir>/campaigns/<id>/`).
  Feature ids are assigned in code, and the store rejects edits, additions and removals
  after definition.
- **Each session is an ordinary run** with a preset one-subtask plan. Its acceptance
  criteria are the feature's checks.
- **Only a definitive independent verification marks a feature passing.** An
  inconclusive verdict counts as an attempt.
- Before each session, passing features are re-verified for regressions.
- Bounds: `max_sessions`, a budget, and per-feature attempts, after which a feature is
  `blocked`.
- The pending session is recorded before it starts. A restart pauses the campaign, and
  resuming finishes the interrupted session first. One replica drives a campaign,
  enforced through a lease.

## Consequences

- An agent can't declare itself done or rewrite the scope.
- Checks are limited to what can be read in files (no browser or test execution, unlike
  the reference demo).
- Campaigns are API-only, and their spend excludes decomposer cost, as runs do.

## Alternatives considered

- **Agent-editable `feature_list.json` with a "don't edit" prompt:** this is the
  reference pattern, and nothing enforces it. Rejected.
- **A `mark_feature` tool the agent calls:** better, but the agent still grades its own
  work. Rejected in favour of verifier-decided status.

KB: [subsystems/campaigns](../kb/subsystems/campaigns.md)

## Later developments (2026-10-30)

*Appended, not rewritten: the decision stands, since agents still can't touch the
ledger and only the verifier marks a feature passing.*

Testing showed that `blocked` and the session/budget limits were dead ends: nothing
could move a campaign past them. Two **operator** (not agent) controls were added:

- **Retry a feature.** A `blocked` or `failing` feature gets fresh attempts, with optional
  written guidance that its next session's instruction carries until it passes. Its
  definition and checks remain immutable.
- **Change limits.** `max_sessions` and the budgets can be changed, and resume reports
  every remaining obstacle at once instead of silently stopping again.

**Considered and not done: letting the operator edit a feature's checks.** That would
make the ledger mutable after definition, which this ADR rules out. If unverifiable
checks turn out to be a real problem, it needs its own ADR that supersedes this one.

