---
title: Context View (Read-Time Snipping & Recall)
last_updated: 2026-10-30
last_verified_sha: 372c48d
sources:
  - src/agentflow/agents/context_view.py
  - src/agentflow/agents/agent.py
  - src/agentflow/config.py
status: current
---

# Context View (Read-Time Snipping & Recall)

> **Why it is built this way:** [ADR-005](../../adr/005-read-time-context-view.md).

An agent's loop keeps its **full** message history. That history is what the session
log records, what `AgentResult.messages` returns, and what continuations and resumes
build on. Each LLM request instead sends a **view** of it,
`ContextSnipper.project(messages)` ([`agents/context_view.py`](../../../src/agentflow/agents/context_view.py)).
Once the prompt grows past a trigger, the view replaces older bulky `tool_result`
contents with a stub. This is Fix 4 of
[`docs/context-optimization-plan.md`](../../context-optimization-plan.md), built as a
read-time projection ("context collapse") so nothing is lost.

## How a turn is built (`Agent._agentic_loop`)

1. `snipper.maybe_snip(messages, last_prompt_tokens, iteration)`. `last_prompt_tokens`
   is the previous call's `usage.input_tokens + cache_creation_input_tokens +
   cache_read_input_tokens`. That sum is the whole prompt, which `input_tokens` alone
   is not when caching is on.
2. If the result exceeds `settings.context_snip_trigger_tokens` (default 100k; `0`
   disables), the boundary moves to the start of the `context_snip_keep_recent`-th newest
   tool-result turn (default 3). Every earlier `tool_result` block of at least
   `context_snip_min_chars` (default 1,000) is stubbed:
   `[Earlier tool result omitted to save context (N chars). Call recall_tool_result(tool_use_id="…") if you need it again.]`.
   The block keeps its `tool_use_id`/`type`, so tool_use ↔ tool_result pairing stays valid.
3. Each snip writes a `context_snipped` session event (`boundary`, `snipped`,
   `freed_chars`, `prompt_tokens`) and an `agent:progress` SSE event with
   `data.context_snipped=true`.
4. `messages=_with_message_cache_breakpoint(snipper.project(messages))`.

**Invariants:**
- **Only `tool_result` content is ever stubbed.** Assistant turns (thinking signatures,
  `tool_use.input`) and plain user turns (instruction, injected messages, finalize
  prompts) are never touched.
- **Cache-stable.** The boundary only moves forward and only when the trigger fires, so
  the projected prefix is byte-identical between snips. Each snip costs one cache
  rebuild, not one per turn.
- **No stale re-trigger.** After moving the boundary, `maybe_snip` ignores measurements
  from calls made before the new view took effect (`_boundary_since_iteration`).
- **First call is estimated.** Before any call, as at the start of a resumed or continued
  invocation with a long inherited history, the size is estimated as `estimate_tokens()`
  (JSON chars / 4).
- If the prompt is over the trigger but no older results are left to stub, it logs a
  warning and returns `None`. That warning is the signal that Fix 5 (LLM auto-compact)
  might be needed.

## Recall

When snipping is enabled, `RECALL_TOOL_PARAM` (`recall_tool_result`, input
`tool_use_id`) is appended to every agent's tool list, including the planner and the
decomposer. It is served **inside the loop** by `Agent._recall_tool_result()`, not by the
global tool registry, because it reads the loop's own unsnipped history (`recall()`). It
emits the usual paired `agent:progress`/`agent:tool_result` events. It is not subject to
`tool_limits`. History results are already capped by `max_result_chars` (Fix 1), so a
recall is bounded too.

## Measuring

`AgentResult.peak_prompt_tokens`, also in every `agent_finished` session event's
`data`, is the largest full prompt of the invocation. Scan session logs for it
before building auto-compaction (plan Fix 5, deliberately not built).

## Testing

`tests/test_context_view.py` covers the snipper unit-wise (stubbing, recent and small
results kept, history unmodified, prefix stability, stale-measurement guard, estimate
path, disabled) and in the agent loop (the view sent vs the history returned, the recall
round-trip, the session event, `peak_prompt_tokens`). Mocked `usage` objects must set
`cache_creation_input_tokens`/`cache_read_input_tokens` to ints: unset `MagicMock`
attributes cannot be compared against the trigger.

## Related

- [session-log](session-log.md) — full transcripts plus `context_snipped` events.
- [architecture](../architecture.md) — lifecycle step 6 and the result-size budget (Fix 1).
