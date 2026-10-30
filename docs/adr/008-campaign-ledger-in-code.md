# ADR-008: Campaigns with a code-enforced feature ledger

**Status:** Accepted
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
