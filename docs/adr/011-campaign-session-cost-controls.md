# ADR-011: Campaign session cost controls

**Status:** Accepted
**Date:** 2026-10-30

## Context

Campaign `2d7391234738` spent $31.25 for 11 of 27 features. The session logs showed
where the money went. The coding agent (Opus 5.5) accounted for 97% of it:

- **Cache rewrites on continuations: ~$8.3 (27%).** The system prompt's second block
  carried the time of day. It sits before the messages in the cache prefix, so every
  continuation that started in a new minute re-wrote the whole conversation at the
  cache-write rate.
- **Truncated whole-file rewrites: ~$3.4 of discarded output**, plus cache rewrites in
  the retries. Most changes to an existing file were full `overwrite`s.
- **The 15-turn limit was spent orienting.** In 11 of 20 sessions the first invocation
  wrote no file, so every session needed a continuation.
- **Half the spend went to sessions that ended unverified.**
- **Every session ran on Opus at `high` effort**, including the skeleton and docs features.

## Decision

- The system prompt carries the **date only**. This applies to every agent.
- `file_write` gains **`replace_text`**: an exact, unique snippet replacement. Session
  rules and the truncation notice point the agent to it.
- Campaign sessions use **campaign settings, not the agent manifest**. They run with
  `max_iterations` 40 and a workspace file map in the instruction. A feature's first
  attempt runs on **Sonnet 5.5 at `medium`** effort. A `failing` feature (one that failed
  a session, regressed, or was retried) **escalates** to the agent's default model (Opus)
  at `high`.
- A campaign gets a **default session budget of $3** unless the request sets one. An
  explicit `null` means no cap. A campaign session that reaches its budget
  **accepts the partial result** instead of asking for more through HITL.
- `replay()` charges the spend of an invocation that was **interrupted by a restart**, from
  the per-call `cost_usd` now logged on each assistant turn.
- `verifier_max_iterations` goes from 8 to 12.

## Alternatives considered

- **A per-invocation frozen timestamp** (record the time at `agent_started` and reuse it
  on resume). This keeps the time of day, but continuations are new invocations, so the
  timestamp would have to travel with the transcript. No agent needs the time of day.
- **A new `file_edit` tool** instead of a `file_write` mode. It would add one more tool
  schema to every prompt and leave six edit modes on the old tool. A mode keeps a single
  write tool.
- **Re-tiering `standard` to Sonnet**. This would change every agent in every run, not just
  campaigns. The planner can already pick tiers for ordinary runs.
- **Escalating on the attempt count only.** A retried blocked feature has its attempts
  reset to 0, but it has evidently been hard. The `failing` status covers both cases.
- **Keeping HITL at the session budget.** Nobody is watching a campaign session, so
  HITL would stall the campaign for `human_input_timeout_s` (30 min) and then accept the
  partial anyway.

## Consequences

- A first attempt costs roughly half as much, and a hard feature pays for Opus only
  once it has failed on Sonnet. One extra session per hard feature is the price of that.
- With the same model and date, a continuation now re-reads its cached prefix instead of
  re-writing it.
- The session budget is a soft cap. It is checked between continuations, so one
  invocation can overshoot it.
- The time of day is no longer in any agent's prompt.
- Older session logs have no `cost_usd`, so their interrupted spend stays uncounted.

## Related

- [ADR-008](008-campaign-ledger-in-code.md): campaigns and the ledger.
- [ADR-003](003-file-backed-session-log.md): the session log `replay()` reads.
- KB: [campaigns](../kb/subsystems/campaigns.md), [session-log](../kb/subsystems/session-log.md).
