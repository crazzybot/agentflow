# ADR-010: Remote agent execution through a dedicated work queue

**Status:** Accepted
**Date:** 2026-10-29

## Context

All agents ran inside the API process. `TaskBus`/`RedisTaskBus` existed as an intended
seam ([ADR-001](001-redis-state-backend.md) deferred worker-pool separation), but they are
keyed per run, with no item correlation, liveness or cancellation. Workers would also have
to know run ids in advance.

## Decision

- With `DISPATCH_MODE=workers`, **only subtask agent invocations** leave the server.
  This covers attempts, fallbacks, continuations and verification repairs, all routed
  through `engine._invoke()`. Planning, scheduling, verification, checkpoints, the
  session log and leases stay on the server.
- **Per-agent Redis queues** (`agentflow:work:<agent_id>`), so a worker can serve a
  subset of agents.
- **Claim plus heartbeat** liveness. An unclaimed item (after a timeout) and a lost
  worker both come back as **failed attempts**, reusing the existing retry and fallback
  logic. Cancellation is a flag the worker's heartbeat acts on.
- **The session log keeps a single writer.** Workers relay `record()` events through
  Redis, and the server appends them with its own sequence numbers
  ([ADR-003](003-file-backed-session-log.md)).
- Requires `STATE_BACKEND=redis`, and a `runs_dir` and workspace shared with the workers.

## Consequences

- Execution scales horizontally, and different hosts can have different sandboxes or
  tools.
- SSE `seq` values emitted by workers can collide with the server's.
- KB ingest from a worker is skipped.
- Relayed events still sitting in Redis are lost if the server owning the run dies.
- The queue isn't reliable (no `BLMOVE` processing list), so a crash between pop and
  claim waits out the claim timeout.

## Alternatives considered

- **Wire the existing `TaskBus`:** it would need a redesign anyway (per-agent queues,
  correlation, liveness). Left unused rather than bent.
- **Celery or another task framework:** a new dependency and runtime model for what
  Redis lists plus heartbeats already cover. Rejected for now.
- **Workers append directly to `session.jsonl`:** two processes appending to one file
  would collide on sequence numbers and risk torn writes. Rejected.

KB: [subsystems/workers](../kb/subsystems/workers.md)
