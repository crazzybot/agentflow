---
title: Resuming Runs & Run Leases
last_updated: 2026-10-30
last_verified_sha: ccf4a77
sources:
  - src/agentflow/orchestrator/resume.py
  - src/agentflow/orchestrator/engine.py
  - src/agentflow/core/lease.py
  - src/agentflow/core/session.py
  - src/agentflow/core/context.py
  - src/agentflow/core/context_redis.py
  - src/agentflow/main.py
  - src/agentflow/api/routes.py
status: current
---

# Resuming Runs & Run Leases

> **Why it is built this way:** [ADR-004](../../adr/004-resume-from-log-with-leases.md).

A run interrupted by a restart is **resumed from its session log**
([session-log](session-log.md)), not marked dead. `OrchestratorEngine.wake(run_id)`
plays the role of Managed Agents' `wake(sessionId)`: the harness is disposable and the
log is the durable state. Per-run **leases** (Redis backend) make resume, reconciliation
and cancel safe when several replicas share `runs_dir`.

## Lifecycle of an interrupted run

1. **Crash.** The process dies mid-run. The session log ends without `run_finished`.
2. **Reconcile** (startup, `await engine.reconcile_orphaned_runs()` from the `lifespan`
   in `main.py`). For each run dir with `meta.json`, skipping runs active here or whose
   lease is live elsewhere, and holding the run's lease while it works:
   - *Session-backed:* orphaned if `replay()` shows no terminal status. Appends
     **`run_interrupted`**, which is *not* terminal, so the run stays resumable. Skipped
     if the run already finished or was already reconciled (`snap.interrupted`, or step
     2's legacy `run_finished: interrupted`), so later restarts don't re-reconcile it.
   - *Legacy (no session log):* orphaned if there is no `report.md`, as before.
   - Both: append a `run:error` SSE event to `events.jsonl`, and write a tombstone
     `report.md` (`# Run Interrupted`, plus a resume hint for session-backed runs) **only
     if no report exists**. A run can die after compiling its real report.
   - Returns the reconciled run ids. If `RESUME_INTERRUPTED_RUNS=true` (default
     **false**, because resuming spends budget unprompted), the lifespan calls
     `schedule_resume()` for each and logs skips (legacy, or claimed elsewhere).
3. **Resume.** `POST /api/runs/{id}/resume` → `await engine.schedule_resume(run_id)`.
   - `RunAlreadyActiveError` (409) is raised if the run is active here or its lease is
     held elsewhere. `RunNotResumableError` (409) is raised if there is no session log,
     no `run_started`, or a terminal status (`complete`/`error`/`cancelled`).
   - The `wake()` task is registered in `_run_tasks` **before** it first runs, so a
     second resume request racing the first is refused.
   - The route returns `{"status": "resumed"}` with the same `run_id`, and clients
     reattach to the same `/stream`.

## What `wake()` restores

`load_resumable()` → `ResumeState.from_snapshot()` (`orchestrator/resume.py`) →
`_run(..., resume=state)`, the same body as `run()` with these differences:

| Concern | Fresh run | Resumed run |
| --- | --- | --- |
| Session events | `run_started` | `run_resumed` (`after_seq`, `finished`, `resuming`); log reopened, `seq` continues |
| Name / meta.json | `_generate_run_name()`, write meta | kept as-is |
| Checkpoints | baseline `"run start"` | same shadow repo reopened; baseline `"run resumed"` |
| Tombstone report | — | `_remove_interrupted_tombstone()` deletes it (only if it is the `# Run Interrupted` placeholder) |
| `RunContext` | empty | `ctx.restore(results, total_cost_usd)` sets (doesn't add) state, budget from the snapshot (includes HITL raises) |
| Plan | classifier/planner | logged plan reused; re-planned only if the crash happened before `plan_created` |
| Scheduler | empty sets | `_resumed_outcomes()` seeds completed/failed from `subtask_finished` events (top-level and micro) |
| Decomposition | decomposer at dispatch | a logged `subtask_decomposed` micro-plan is re-run as-is, without the decomposer |
| Mid-flight subtask | fresh `Agent.run` | first attempt passes `resume_messages` = its transcript; retries start over |

**Transcript selection.** For each dispatched-but-unfinished subtask, take the newest
unfinished invocation with the dispatch's `task_id`. Retries start clean, and
continuations re-log their prefix, so the newest has the fullest history.
`_trim_transcript()` drops a trailing assistant turn containing `tool_use`, because its
results were never logged and the API requires every `tool_use` to be answered. The
model re-asks for what it still needs and sees the workspace as the interrupted tools
left it. If only the opening instruction survives, the subtask is dispatched fresh.
`take_transcript()` hands a transcript out once, and discards it if its agent differs
from the subtask's (a fallback was running). A subtask with a transcript skips
decomposition: its transcript already contains any decomposer context.

**Known gaps:**
- The crashed invocation's spend is not charged: `cost_added` is only written when an
  invocation ends.
- Single-subtask decompositions are not logged as micro-plans. Their context is
  already in the transcript.
- Runs whose logs predate `subtask_finished` re-run every subtask without a stored
  outcome.

## Leases (`core/lease.py`)

`run_leases` is built by `_make_run_leases()` from `STATE_BACKEND`:

- **`RunLeases`** (memory): no-ops. `acquire` is always True, nothing is held elsewhere,
  and `request_cancel` returns False. A single process owns every run.
- **`RedisRunLeases`**: `run:{id}:lease` = this process's random `owner_id`, with a
  `RUN_LEASE_TTL_S` TTL (default 15 s).
  - `acquire` is `SET NX PX`, re-entrant for the same owner. A fresh acquisition deletes
    any stale `run:{id}:cancel` left by a dead owner.
  - `release` and renew are Lua compare-and-delete / compare-and-`PEXPIRE`, so a replica
    never touches another replica's lease. `release` swallows `RedisError` with a
    warning (it runs in cleanup; the TTL clears the lease).
  - `keep_alive(run_id, run_task)` runs beside the run and renews every TTL/3. If the
    cancel flag is set, it cancels the run. If the renew finds the lease taken or gone,
    it cancels with `msg=LEASE_LOST`. A `RedisError` during renew is logged and retried
    (a transient outage must not kill the run).
  - `request_cancel` sets `run:{id}:cancel` (TTL `redis_key_ttl`) only if a lease exists.

Campaigns reuse leases under the id `campaign-<campaign_id>`, so one replica drives a
campaign, and reuse `wake()`/`wait_for()` to finish a session interrupted mid-campaign
(see [campaigns](campaigns.md)).

Engine integration:
- `_run()` acquires the lease **first**, before touching the log (otherwise
  `RunAlreadyActiveError`), starts `keep_alive`, and cancels it and releases in `finally`.
- `request_cancel()` (used by `POST …/cancel`) returns `"cancelled"` (local),
  `"cancel_requested"` (another replica owns it; takes effect within about TTL/3 s), or
  `None` → 404.
- **Lost lease = stand down.** On a `CancelledError` carrying `LEASE_LOST`, the engine
  sets `final_status = None`. It writes no `run_finished` into the log the new owner is
  appending to, emits no `run:cancelled`, and marks the emitter `done` locally instead of
  `close()`, because the Redis sentinel would end the new owner's stream.

**Still not covered:**
- Reconciliation only runs at **startup**. A replica that dies while the others keep
  running is not detected until some replica restarts; a periodic sweep is the next step.
- The session log is a shared file. A stood-down owner can still write a few events with
  duplicate `seq` before its cancellation lands.

## Testing

- `tests/test_resume.py` simulates a real crash: run, block mid-subtask, cancel, then
  strip the trailing `run_finished`. It then checks reconcile → `wake()`.
- `tests/test_lease.py` uses `fakeredis[lua]` (dev dependency) for the Lua scripts, with
  two `OrchestratorEngine`s on one `FakeServer` acting as two replicas.
- `tests/conftest.py` autouse-patches the engine's `run_leases` to the in-process
  `RunLeases`. Lease calls are awaited directly, so a developer `.env` with
  `STATE_BACKEND=redis` and no server would otherwise fail every engine test.

## Related

- [session-log](session-log.md) — the log and `replay()` this builds on.
- [redis-backend](redis-backend.md) — key layout, including `…:lease` / `…:cancel`.
- [architecture](../architecture.md) — run lifecycle and API surface.
