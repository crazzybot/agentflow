---
title: Session Log (Durable Run Record)
last_updated: 2026-10-29
last_verified_sha: d09693f
sources:
  - src/agentflow/core/session.py
  - src/agentflow/orchestrator/engine.py
  - src/agentflow/agents/agent.py
  - src/agentflow/api/routes.py
status: current
---

# Session Log (Durable Run Record)

Every run writes an append-only log to `<runs_dir>/<run_id>/session.jsonl`
([`core/session.py`](../../../src/agentflow/core/session.py)). The log is the durable
record a run can be reconstructed from. Its design follows the "session" component of
Anthropic's Managed Agents architecture: a log that lives outside the harness and the
context window, read back in slices.

How it differs from the other per-run files:

| File | Written | Purpose |
| --- | --- | --- |
| `session.jsonl` | **always** | Source of truth: inputs, plan, results, cost, HITL, full agent transcripts. |
| `events.jsonl` | only with `capture_events` | Copy of the SSE stream, a UI projection (truncated messages, no history). |
| `results.jsonl` | only with `capture_results` | Final `AgentResult` per subtask, including `messages`. |

## Event model

`SessionEvent` has the fields `run_id`, `seq` (1-based, per run, gap-free), `ts`
(ms), `type`, `agent_id`, `subtask_id`, `invocation_id` and `data`. The event types
(`SessionEventType`) are:

| Type | Recorded by | `data` |
| --- | --- | --- |
| `run_started` | `engine.run()` | `task`, `user_context`, `budget_usd`, `workspace` |
| `plan_created` | `engine.run()` (planner and direct branches) | `plan`, `direct` |
| `subtask_dispatched` | `_dispatch_subtask()` | `subtask`, `task_id` (the envelope's), `budget_usd` |
| `subtask_decomposed` | `_dispatch_subtask()` when the decomposer splits | `micro`, as dispatched (decomposer context included) |
| `subtask_finished` | `_execute_plan()` / `_run_micro_subtasks()` | `ok`, optional `reason` (`upstream_failed`). The scheduler's own completed/failed decision. |
| `result_stored` | `_store_result()` | `result` (AgentResult **without** `messages`) |
| `cost_added` | `_add_cost()` | `cost_usd`, `task_id` |
| `human_input_requested` / `_received` | `_request_budget_increase()`, agent `ask_user` | request / response, new `budget_usd` |
| `agent_started` | `Agent.run()` | `task_id`, `resumed` |
| `message_appended` | `_agentic_loop()` | `message`; assistant turns add `model`, `stop_reason`, `usage`; injected user messages add `injected` |
| `message_discarded` | `_agentic_loop()` on `max_tokens` | `reason`. Drops the invocation's last message. |
| `context_snipped` | `_agentic_loop()` | `boundary`, `snipped`, `freed_chars`, `prompt_tokens`. Affects only the model's view; transcripts in the log stay full ([context-view](context-view.md)). |
| `agent_finished` | `Agent.run()` (also on exceptions) | `status`, `cost_usd`, tokens, `peak_prompt_tokens`, `duration_ms`, `error` |
| `run_interrupted` | `reconcile_orphaned_runs()` | `reason`. Not terminal: the run stays resumable. |
| `run_resumed` | `wake()` | `after_seq`, `finished`, `resuming`. Clears interrupted/status in `replay()`. |
| `run_finished` | `_run()` `finally` | `status`: `complete`/`error`/`cancelled`. Not written when the run stands down after losing its lease. Older logs may carry `interrupted` from step 2's reconciliation, which `replay()` still treats as resumable. |

**Invocations.** Each `Agent.run()` call gets a fresh `invocation_id`. This covers
subtasks, retries, continuations, fallbacks, the planner and the decomposer. The
invocation's transcript is the sequence of its `message_appended`/`message_discarded`
events. A resumed invocation (`_continue_partial`) re-logs the inherited prefix, so
each transcript can be replayed on its own. The cost is some duplication on
continuations. Link a transcript to its subtask through
`agent_started.data.task_id` == `subtask_dispatched.data.task_id`.

**Cost parity.** `cost_added` is written exactly where `ctx.add_result_cost()` was
called, so replayed cost matches `ctx.total_cost_usd()`. That means planner and
decomposer spend is **not** included, which matches today's `RunContext`. Their cost
is still visible on their own `agent_finished` events.

## Writing

- The engine arms the `_current_session` ContextVar in `run()`. Asyncio tasks copy the
  context, so the planner, subtasks, decomposer and KB dispatch all log without
  plumbing. `record()` is a **no-op outside a run**, so unit tests that call
  `Agent.run()` directly write nothing.
- `RunSession.append()` is async (so a networked backend can slot in later), but the
  file backend completes synchronously. There is no `await` between assigning `seq` and
  writing the line, so concurrent subtasks cannot interleave or reorder lines.
  `append_sync()` exists for synchronous callers (startup reconciliation).
- Reopening a log (`RunSession(run_id)` on an existing file) continues numbering from
  the last `seq`.
- Write failures propagate, except the terminal `run_finished` in `engine.run()`'s
  `finally`. That one is logged with `logger.exception` so it never skips run cleanup.
- Payloads must be JSON-serialisable. Message content is already plain dicts via
  `_to_dict_content()`, and thinking `signature` fields are preserved.

## Reading

- `RunSession.events(after_seq=, types=, invocation_id=)`, `read_session(run_id)`
  and `filter_events()` are the `getEvents`-style slice API.
- `GET /api/runs/{id}/session` exposes the same filters (`type` is repeatable).
  `RunInfo.has_session` flags runs that have a log.
- Reading tolerates a **torn final line** (process killed mid-write), which is skipped
  with a warning. A malformed line anywhere else raises, because the rest is suspect.
- `replay(run_id, events) -> RunSnapshot` folds a log into run state: inputs, `plan`,
  `dispatched` (subtask → task_id), `results`, `total_cost_usd`, `budget_usd` (after
  HITL raises), `invocations` (transcript plus finished/status), `unfinished_invocations`,
  terminal `status` (`None` = still in flight or killed), and `last_seq`.
  `tests/test_session.py` checks that a replayed engine run matches the live
  `RunContext` results and cost.

## Backends and limits

- The log is **file-backed under both `STATE_BACKEND`s.** Redis keys expire after
  `redis_key_ttl`, so Redis can't be the durable record, and replicas must already share
  `runs_dir` for meta, reports and followups.
- Every `events()` call re-reads the whole file. That's fine at current run sizes; add
  an offset index if logs get large.
- The log holds full tool results and prompts. Treat `runs_dir` as being as sensitive
  as the workspace.
- `RunSnapshot` also carries `finished`, `micro_plans`, `interrupted` and `resumable`.
  [resume-and-leases](resume-and-leases.md) builds `wake(run_id)` on them, restoring
  `RunContext` from the snapshot when a run is resumed. A live run still keeps
  `RunContext` in memory rather than deriving it from the log.

## Related

- [architecture](../architecture.md) — lifecycle steps 2, 6 and 8 and the run directory layout.
- [redis-backend](redis-backend.md) — why the log is not a Redis Stream.
