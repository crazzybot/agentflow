# ADR-004: Resume interrupted runs from the log; per-run ownership leases

**Status:** Accepted
**Date:** 2026-10-29

## Context

A restart tombstoned every in-flight run. With several replicas there were two more
hazards: a replica starting up tombstoned runs still alive on other replicas, and
cancel only worked on the replica executing the run.

## Decision

- **Resume.** Reconciliation marks a run `run_interrupted`, which is not terminal.
  `wake(run_id)` replays the log: it restores `RunContext`, reuses the plan, seeds the
  scheduler from `subtask_finished` events, and continues mid-flight subtasks from
  their newest transcript. A trailing tool request whose results were never logged is
  dropped.
- **Opt-in auto-resume** (`RESUME_INTERRUPTED_RUNS=false` by default), because resuming
  spends budget unprompted.
- **Leases** (Redis): `run:{id}:lease` holds the owner's process id with a short TTL
  renewed by a keep-alive. Reconcile and resume skip runs with a live lease. Cancel
  works across replicas through a flag the owner's keep-alive acts on.
- **An owner that loses its lease stands down** without writing a terminal event or an
  end-of-stream sentinel.

## Consequences

- Restarts no longer lose work, and replicas no longer tombstone each other's runs.
- The crashed invocation's spend isn't charged: cost is recorded when an invocation
  ends.
- Reconciliation still only runs at startup. A replica that dies while the others stay
  up is detected only at the next restart; a periodic sweep is future work.

## Alternatives considered

- **Re-run interrupted subtasks from scratch:** simpler, but it wastes the work already
  done and isn't what the log was built for. Rejected.
- **Auto-resume by default:** surprise spend after a deploy. Rejected in favour of opt-in.

KB: [subsystems/resume-and-leases](../kb/subsystems/resume-and-leases.md)
