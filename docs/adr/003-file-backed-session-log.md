# ADR-003: A file-backed, append-only session log is the run's source of truth

**Status:** Accepted
**Date:** 2026-10-29

## Context

Run state lived in memory (`RunContext`, each agent's message list). `events.jsonl` was
optional and held only a lossy SSE projection. A crash lost everything needed to
continue a run. Managed Agents' design separates a durable *session* (an event log)
from the harness and the sandbox.

## Decision

- Every run always writes `<runs_dir>/<run_id>/session.jsonl`: typed, sequenced events
  covering inputs, the plan, dispatches, results, cost, HITL, and **every agent
  invocation's full message history**.
- Writes go through a `ContextVar`, and `record()` is a no-op outside a run. The
  read API takes slices (`after_seq`, `types`, `invocation_id`), and `replay()` folds a
  log back into a `RunSnapshot`.
- **The log is a file under both state backends**, not a Redis Stream.

## Consequences

- Resume, campaigns (absorbing session outcomes) and audits all read one durable
  record ([ADR-004](004-resume-from-log-with-leases.md),
  [ADR-008](008-campaign-ledger-in-code.md)).
- The log stores full prompts and tool output, so `runs_dir` is as sensitive as the
  workspace.
- A resumed invocation re-logs its inherited history, which duplicates some data.
- The interface is async even though the file backend is synchronous, so a networked
  backend can slot in later.
- Multi-process writers need a single-writer design ([ADR-010](010-remote-agent-workers.md)).

## Alternatives considered

- **Redis Streams in Redis mode:** keys expire after `REDIS_KEY_TTL`, so Redis can't be
  the durable record. Replicas already share `runs_dir` for meta, reports and
  followups. Rejected.
- **Extend `events.jsonl`:** it's opt-in and holds a UI projection (truncated messages).
  Mixing in transcripts would conflate the two. Rejected.

KB: [subsystems/session-log](../kb/subsystems/session-log.md)
