# ADR-005: Bound context with a read-time view, not history mutation; defer auto-compact

**Status:** Accepted
**Date:** 2026-10-29

## Context

A long agent loop resent every tool result on every turn. The context-optimization plan
(Fix 4: snip, Fix 5: LLM auto-compact) proposed trimming, but trimming the history
itself would destroy what the session log, continuations and resume rely on.

## Decision

- Agents keep the full history. Each request sends `ContextSnipper.project(messages)`:
  once the measured prompt passes `CONTEXT_SNIP_TRIGGER_TOKENS`, older bulky
  `tool_result` contents are replaced by stubs naming their `tool_use_id`. A
  loop-local `recall_tool_result` tool restores any of them.
- The snip boundary only moves forward, and only when the trigger fires, so the
  prompt-cache prefix stays stable.
- **LLM auto-compaction (Fix 5) is deliberately not built.** `peak_prompt_tokens` is
  recorded per invocation so the need can be measured first.

## Consequences

- No information is lost. Each snip costs one cache rebuild, not one per turn.
- After a snip, decisions wait for usage measured with the new view, which avoids
  re-snipping on stale sizes.
- Assistant turns are never edited, so thinking signatures and `tool_use.input` stay
  intact.
- Every agent, the planner included, gets the extra recall tool.

## Alternatives considered

- **Drop old tool results from the history:** this is the plan's literal Fix 4, and it
  is irreversible. Rejected.
- **Build auto-compact now:** costs an LLM call, and the plan itself says to measure
  first. Deferred.

KB: [subsystems/context-view](../kb/subsystems/context-view.md) · Plan: [context-optimization-plan](../context-optimization-plan.md)
