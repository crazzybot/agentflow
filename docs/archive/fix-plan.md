# AgentFlow — Run-Analysis Fix Plan

**Source analyses:** runs `68300c0d` (AI Agents Research) and `56a39815` (RL Trends)
**Date:** 2026-07-10

---

## Fixes

### Fix 1 — Hard-enforce per-tool call budgets (HIGH impact)
**Problem:** `fetch_url` was called 14× (run 1) and 12× (run 2) against a manifest-stated soft limit of 5. `arxiv_search` was called 21× (run 2) with no limit at all. Soft system-prompt instructions are consistently ignored under task pressure.

**Changes:**
- `src/agentflow/core/models.py` — Add `tool_limits: dict[str, int] | None = None` to `AgentManifest`.
- `src/agentflow/agents/agent.py` — Add `_checked_call_tool()` method that tracks per-tool call counts and returns a hard error result when a limit is exceeded. Use this in the agentic loop instead of `_call_tool`.
- `manifests/research_agent.yaml` — Add `tool_limits: {fetch_url: 5, arxiv_search: 8}`.

### Fix 2 — file_write returns line count (MEDIUM impact)
**Problem:** After writing, agents probe `start_line=550` blindly. A 440-line file returns empty — wasting one tool call and requiring a corrective re-read. A 669-line file requires knowing the line count to target the end correctly.

**Changes:**
- `src/agentflow/tools/builtin.py` — `overwrite` and `append` modes in `_file_write` return `"Wrote N lines (M chars) to path"` so agents can compute a targeted end-verification offset.

### Fix 3 — arxiv_search default max_results: 10 → 5 (MEDIUM impact)
**Problem:** Each search with `max_results=10` loads 10 abstracts. At 21 searches that's 210 abstracts in context; most are never acted on. This inflated the inherited `prior_messages` context for the WriterAgent to 496k cache-read tokens.

**Changes:**
- `src/agentflow/tools/arxiv_search.py` — Change function default to `max_results: int = 5`.
- `src/agentflow/tools/builtin.py` — Change schema default and description to 5.

### Fix 4 — ResearchAgent manifest: parallel-search and pre-planning guidance (MEDIUM impact)
**Problem:** All web_search/arxiv_search calls were issued one per LLM turn (sequential). Two web searches were near-duplicates of each other (no upfront query planning).

**Changes:**
- `manifests/research_agent.yaml` — System prompt additions:
  1. Pre-search planning: list all queries before executing any.
  2. Parallel batching: emit independent searches as multiple tool_use blocks in one response.
  3. Update fetch_url note to reflect the new hard enforcement.
  4. Add arxiv_search budget note.

### Fix 5 — WriterAgent manifest: context-first and single-pass write guidance (HIGH impact)
**Problem:** WriterAgent was instructed to "read the file" even though the file content was already in its inherited `prior_messages` context. It re-read 663–669 lines in 3–4 chunks, then in run 2 did 2 more mid-flow spot-check re-reads before writing. Also did inefficient post-write verification.

**Changes:**
- `manifests/writer_agent.yaml` — System prompt additions:
  1. Context-first: if a file's content appears in conversation context from a prior agent, do not re-read it.
  2. Single-pass: read all source material completely before writing; do not interleave reads with writing.
  3. Efficient verification: after writing, use `file_read` lines 1–30 + pattern match for headings only.

### Fix 6 — Planner: don't tell downstream agents to re-read upstream output files (HIGH impact)
**Problem:** The planner instructed WriterAgent to "Read the file 'X.md'" — but the `prior_messages` mechanism already put that file's content in the downstream agent's context. The downstream agent dutifully re-read it anyway.

**Changes:**
- `src/agentflow/orchestrator/planner.py` — Add guidance to `_SYSTEM_PROMPT_BASE`: when a subtask's `dependsOn` references another subtask that writes files, the downstream instruction must NOT say to read those files. Instead direct the agent to synthesise from its context.

---

## Execution order

1. `models.py` — tool_limits field
2. `agent.py` — _checked_call_tool + counter
3. `builtin.py` — file_write line count
4. `arxiv_search.py` + `builtin.py` — max_results default
5. `research_agent.yaml` — system prompt
6. `writer_agent.yaml` — system prompt
7. `planner.py` — system prompt
8. Tests — tool_limits enforcement + file_write line count
9. `uv run pytest`
