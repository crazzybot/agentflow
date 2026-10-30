# ADR-006: Independent read-only verification of acceptance criteria

**Status:** Accepted
**Date:** 2026-10-29

## Context

A subtask counted as done when its own agent said so: for example, `CodeAgent`
returning `files_missing: []`. Self-report is the weakest form of evidence.

## Decision

- The planner may attach 1–5 **file-checkable** `acceptanceCriteria` to a subtask.
- A separate **read-only** verifier agent (economy tier, `file_read`/`bash_exec_readonly`)
  checks them against the workspace. It sees the agent's reported output only as a claim
  to verify, never the agent's conversation.
- Unmet criteria go back to the producing agent, which continues its own transcript to
  fix them (`VERIFICATION_MAX_REPAIRS`). A result that still fails is downgraded to
  partial, with the unmet criteria reported.
- **Within a run, an inconclusive verdict passes (fail-open)**, so a broken checker never
  sinks good work. **Campaigns are fail-closed**: only a definitive pass marks a feature
  passing ([ADR-008](008-campaign-ledger-in-code.md)).
- Decomposed subtasks are verified on their promoted result. This was a bug, found and
  fixed after the initial implementation.

## Consequences

- A verified subtask costs one more small model call, plus a call per repair round.
- Criteria must be checkable by reading files. "Tests pass" can't be checked by a
  read-only verifier.

## Alternatives considered

- **Let the verifier run tests/commands:** stronger checks, but the planner (which reads
  untrusted content) would effectively author commands that get executed. Deferred.
  Ops-defined verification commands per manifest would be the safe form.
- **Fail-closed in runs:** a flaky verifier would downgrade correct work. Rejected for
  runs, adopted for campaigns, where the ledger must only record confirmed facts.

KB: [subsystems/verification-and-checkpoints](../kb/subsystems/verification-and-checkpoints.md)
