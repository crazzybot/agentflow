---
title: Worker Processes (Remote Agent Execution)
last_updated: 2026-10-30
<<<<<<< HEAD
last_verified_sha: 7486398
=======
last_verified_sha: 629fee4
>>>>>>> 9cf66a6d8f2e01379210f7c641a4e445227231f8
sources:
  - src/agentflow/orchestrator/workers.py
  - src/agentflow/orchestrator/engine.py
  - src/agentflow/core/session.py
  - src/agentflow/core/models.py
  - src/agentflow/cli/__init__.py
  - src/agentflow/config.py
status: current
---

# Worker Processes (Remote Agent Execution)

> **Why it is built this way:** [ADR-010](../../adr/010-remote-agent-workers.md).

With `DISPATCH_MODE=workers`, the API server stays the **brain**: planning, scheduling,
decomposition, verification, checkpoints, the session log and leases all stay there.
Each **subtask agent invocation** goes to an `agentflow worker` process (one of many
"hands"), possibly on another machine with its own sandbox
([sandbox-and-policies](sandbox-and-policies.md)). Code:
[`orchestrator/workers.py`](../../../src/agentflow/orchestrator/workers.py).

## The switch point

Every subtask invocation in the engine goes through `OrchestratorEngine._invoke(run_id,
agent_id, envelope, emitter, ctx, resume_messages)`: attempts and transcript resume in
`_dispatch_subtask`, the fallback agent, `_continue_partial`, and verification repairs.
`_invoke` calls the local `Agent.run` when `self._remote is None` (`DISPATCH_MODE=local`,
the default), and otherwise `RemoteInvoker.invoke()`. The planner, decomposer, verifier
and campaign feature planner always run in the server. `_make_remote_invoker()` rejects
an unknown mode, and `workers` without `STATE_BACKEND=redis`, when the engine is
constructed, so the app fails at import.

## Protocol (Redis)

| Key | Written by | Purpose |
| --- | --- | --- |
| `agentflow:work:<agent_id>` | engine `RPUSH` / worker `BLPOP` | Queued `WorkItem`s: `item_id`, `run_id`, `agent_id`, `envelope`, `resume_messages`, `workspace` (relative to the workspace root, taken from `_current_workspace`) |
| `work:<item>:claimed` | worker | Worker id; TTL = 3 × `WORKER_HEARTBEAT_S`, refreshed each beat |
| `work:<item>:events` | worker (`RemoteSessionWriter`) | Relayed session events `{type, agent_id, subtask_id, invocation_id, data}` |
| `work:<item>:result` | worker | The final `AgentResult` (one entry) |
| `work:<item>:cancel` | engine | Stop request, checked on every worker heartbeat |

**Engine wait loop** (`RemoteInvoker.invoke`):
- It runs `BLPOP [events, result]` with a 1 s timeout. Redis checks keys in order, so
  queued events drain before the result, and each relayed event is appended via
  `record()`.
- **Liveness.** After a claim was seen, a missing `claimed` key means the worker was lost,
  which returns a failed `AgentResult("worker lost …")`. If the item is never claimed,
  then after `WORKER_CLAIM_TIMEOUT_S` (60) it is `LREM`ed from the queue and returns a
  failed result ("no worker claimed … is a worker serving X running?"). If the `LREM`
  finds nothing, a worker just took the item, and waiting continues.
- **These failures are ordinary failed attempts**, so the existing retries, fallback
  agent, file rollback and reporting apply unchanged.
- **Cancellation** of the waiting task (run cancel, or `wait_for` task timeout) sets
  `cancel`, withdraws the item if it is still queued, and re-raises. Event and result
  keys are deleted in `finally`.

**Worker** (`Worker.handle`):
- It claims the item, runs `_execute()` as a task beside a `_heartbeat()` that refreshes
  the claim and cancels the agent task when `cancel` appears, then pushes the result. An
  agent cancelled on request pushes nothing.
- On worker shutdown (`current_task().cancelling()`), it cancels the agent and re-raises.
  The claim is deleted, so the engine sees a lost worker.
- `_execute()` sets, for the invocation:
  - `_current_workspace`: the shared workspace path;
  - `_current_session`: a `RemoteSessionWriter`;
  - `_current_sandbox`: the worker's own `SandboxProvider`, closed afterwards;
  - `_current_sink`: `artifacts.jsonl` in the shared `runs_dir`.
- It gets the emitter from `stream_registry.connect(run_id)` (Redis Streams, so SSE from
  the worker reaches every replica; a local emitter if absent) and the context from
  `context_store.connect(run_id)` (user messages and HITL over Redis), then runs `Agent`
  from the worker's own registry and `LLMClient`.

**Single log writer.** Workers never touch `session.jsonl`. `core.session.SessionWriter`
is the protocol `record()` targets: a `RunSession` in the server, a
`RemoteSessionWriter` in workers. The server assigns `seq`, so sequence numbers stay
unique and gap-free.

## Running

```bash
DISPATCH_MODE=workers STATE_BACKEND=redis uv run uvicorn agentflow.main:app --port 8002   # server
uv run agentflow worker [--agents CodeAgent,ResearchAgent] [--concurrency 4]  # any number
```

The CLI refuses to start without `STATE_BACKEND=redis`, and `Worker` rejects agent ids
missing from its own `manifests/`.

## Requirements and limits

- **Shared filesystem.** Workers and the server must share `runs_dir` (artifacts) and the
  workspace root (agents edit files; checkpoints and the verifier read them on the server).
- **SSE `seq` collisions.** Events emitted by a worker use that process's own
  `RedisStreamEmitter` counter, so SSE `seq` values can collide with the server's. Order
  by Redis stream position instead.
- **KB ingest from a worker** (`download_document`) is skipped: `_kb_dispatch_fn` isn't
  armed there.
- **Server death loses in-flight events.** If the server owning a run dies, relayed events
  still sitting in Redis are lost. On resume, the subtask continues from the last
  transcript the server had appended.
- **Unreliable queue.** A crash between a worker's `BLPOP` and its claim leaves the item to
  the claim timeout. A reliable-queue `BLMOVE` would close that window.
- **The old `TaskBus` isn't used for this.** It is per-run, without correlation, liveness
  or cancel; it still exists but no longer marks the worker seam.

## Testing

`tests/test_workers.py` uses fakeredis, with a real `Worker` and a real engine (`_remote =
RemoteInvoker`) sharing Redis-backed stream and context stores. It covers:
- an end-to-end remote subtask, with the transcript relayed into the log;
- an unclaimed item withdrawn and failed (a worker that serves only another agent);
- a dead worker → failed attempt;
- run cancel → the remote agent is cancelled and the worker keeps serving;
- relay format, config validation, and local mode never touching the queue.

## Related

- [redis-backend](redis-backend.md) — the shared state that makes cross-process SSE, messages and HITL work.
- [session-log](session-log.md) — the log the server keeps writing.
- [sandbox-and-policies](sandbox-and-policies.md) — each worker's own sandbox.
