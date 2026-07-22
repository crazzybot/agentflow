# AgentFlow — Context Optimization Plan

**Source:** comparison against Claude Code's context-management architecture (arXiv 2604.14228v2,
"Dive into Claude Code" — source-level reverse-engineering study of Claude Code's `queryLoop()`
context shapers)
**Date:** 2026-07-21
**Related:** [`docs/presentations/agentflow_gap_analysis.md`](presentations/agentflow_gap_analysis.md)
Gap 6 (uncontrolled context growth, no summarization/truncation) — this plan is the concrete
implementation of that gap's fix, informed by how Claude Code solves the same problem.

---

## Reference model: Claude Code's shaper cascade

Claude Code runs five context-reduction passes on every turn, cheapest-first, escalating only if
pressure survives the previous pass — never pay for an LLM call to save context if a free, local
operation would have done it:

1. **Budget reduction** — per-message size cap on tool results; oversized output replaced with a
   content reference. Tools can opt out of the cap.
2. **Snip** — lightweight trim of older history segments. Known footgun: if context-size tracking
   reads a cached/stale token-usage field, snip's savings become invisible unless explicitly
   propagated.
3. **Microcompact** — finer-grained compression, always runs a time-based pass.
4. **Context collapse** — a **read-time projection**, not a mutation: the full transcript stays
   intact underneath; only what's shown to the model this turn shrinks.
5. **Auto-compact** — the only tier that costs an LLM call: summarizes everything before a boundary
   and splices the summary in.

Recovery mechanisms layered on top: max-output-token retry escalation, reactive (once-per-turn)
compaction near capacity, and an explicit fallback order on a hard context-length error (structural
recovery first, summarization second, terminate only if both fail).

## Where AgentFlow actually grows unbounded today

Two independent growth vectors, confirmed against current source:

- **Within one agent's own ReAct loop** (`Agent._agentic_loop()`) — message history accumulates
  every tool result and file content verbatim across iterations, with no cap.
- **Across the DAG** — `RunContext.build_prior_results()` / `build_upstream_artifacts()`
  concatenate upstream agents' raw text and structured JSON verbatim into every downstream agent's
  *initial* message. This is AgentFlow-specific (Claude Code has no DAG of heterogeneous agents
  feeding each other) and compounds with DAG depth — the highest-leverage target.

---

## Fixes

### Fix 1 — Tool-result budget capping (HIGH impact, LOW effort)
**Problem:** Tool results (especially `bash_exec`/`python_exec` stdout) are appended to message
history in full, unbounded. This is the single biggest per-iteration growth source.

**Changes:**
- `src/agentflow/tools/registry.py` — Add `max_result_chars: int | None = None` to `ToolDefinition`
  (default a few thousand chars; `None` is an explicit unbounded opt-out for tools whose full
  output downstream logic depends on, e.g. planner/decomposer JSON-emitting tools).
- `src/agentflow/agents/agent.py` — In `_checked_call_tool()`, after the tool handler returns,
  truncate results over `max_result_chars` and replace with a preview + pointer. For
  `bash_exec`/`python_exec`, write full stdout to a file under the run's workspace and return
  `"[N chars truncated, full output: <path>]"` — this extends the existing pattern already used by
  `file_write`, which returns a path rather than inlining content.
- `src/agentflow/tools/builtin.py` — Set `max_result_chars` on the relevant builtin
  `ToolDefinition`s (`bash_exec`, `python_exec`, `web_fetch`, etc.); leave structured/short-output
  tools uncapped.

### Fix 2 — Upstream-context pointer-only injection (HIGH impact, LOW-MEDIUM effort)
**Problem:** `build_prior_results()` / `build_upstream_artifacts()` inline full upstream text and
JSON into the `<upstream_context>` block of every downstream agent's first message. Because this
fires once per DAG edge, it multiplies with graph depth — worse than the within-loop growth in
Fix 1 for deep plans.

**Changes:**
- `src/agentflow/core/models.py` (or wherever `RunContext.build_prior_results()` /
  `build_upstream_artifacts()` live) — Add a size threshold (e.g. ~2000 chars). Content under the
  threshold stays inline (as today); content over it is replaced with a short prose summary +
  file-path reference, letting the downstream agent `file_read` it only if actually needed.
- Keep the existing prose-summary behavior for artifacts that already have one; this only changes
  the fallback for large *raw* text/JSON that's currently dumped verbatim.

### Fix 3 — Recovery mechanics: output-token retry + fallback model (MEDIUM impact, LOW effort)
**Problem:** No escalation exists today beyond the Anthropic SDK's own 429/500 retries. A turn cut
off at `max_tokens` is silently truncated; a model failing repeatedly on a given agent has no
fallback.

**Changes:**
- `src/agentflow/agents/agent.py` — In the agentic loop, detect a response truncated at
  `max_tokens` and retry once with an escalated cap (bounded, matching Claude Code's
  `MAX_OUTPUT_TOKENS_RECOVERY_LIMIT` pattern — cap the retry count).
- `src/agentflow/core/models.py` — Add `fallback_model: str | None = None` to `AgentManifest`.
- `src/agentflow/agents/agent.py` / `src/agentflow/llm/` — On repeated context-length or rate
  failures for a given agent run, switch remaining turns to `fallback_model` if set.
- `manifests/*.yaml` — Set `fallback_model` on agents most prone to long-running loops (code,
  research).

### Fix 4 — Snip-equivalent: in-loop history trimming (MEDIUM impact, MEDIUM effort)
**Problem:** Even with Fix 1 capping individual results, a long-running agent's cumulative history
still grows unbounded across many iterations.

**Changes:**
- `src/agentflow/agents/agent.py` — In `_agentic_loop()`, once iteration count or an estimated
  token count (derivable from `LLMClient`'s existing `UsageStats`) crosses a soft threshold, drop
  the oldest `tool_result` messages from the working history — keep assistant reasoning text,
  replace bulky results with a one-line `"[earlier tool result omitted]"` marker. No LLM call
  required.
- Ensure whatever tracks context/budget size explicitly accounts for the freed space rather than
  reading a stale cached token count (the exact bug Claude Code's own snip layer had to guard
  against).

### Fix 5 — Real auto-compact for long-running agents (LOWER priority, HIGHEST effort)
**Problem:** For agent types with many iterations (code/research agents using
`decomposition_prompt`), Fixes 1–4 may still not be enough to keep a single loop's context bounded
over very long runs.

**Changes:**
- `src/agentflow/agents/agent.py` — Add an optional compaction pass: one extra LLM call that
  summarizes everything before a tracked "last-compacted-message-index" and replaces those raw
  messages with the summary. Unlike Claude Code, AgentFlow doesn't need persisted UUID boundary
  chains for this (runs are short-lived) — a simple in-memory index per agent run is sufficient
  unless/until a true run-resume feature is built, at which point that index becomes the natural
  resume marker.
- Only trigger this tier if Fixes 1–4 demonstrably fail to keep context under threshold for a given
  agent — measure via `UsageStats` before building this; don't build it speculatively.

---

## Execution order

1. `tools/registry.py` — `max_result_chars` field on `ToolDefinition`
2. `agents/agent.py` — `_checked_call_tool()` truncation + pointer-replacement logic
3. `tools/builtin.py` — set caps on `bash_exec`/`python_exec`/`web_fetch` etc.
4. `core/models.py` (`RunContext` upstream-context builders) — size-threshold pointer injection
5. `core/models.py` — `fallback_model` field on `AgentManifest`
6. `agents/agent.py` — max-output-token retry + fallback-model switch
7. `manifests/*.yaml` — set `fallback_model` on long-running agent types
8. Tests — tool-result truncation, upstream-context pointer threshold, fallback-model switch
9. `uv run pytest`
10. Measure context/token usage on representative runs (via existing `UsageStats`) before deciding
    whether Fix 4 (in-loop snip) and Fix 5 (real auto-compact) are needed
