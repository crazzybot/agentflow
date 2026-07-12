# Session Log

Breadcrumb trail of per-session decisions, discoveries, and open questions.
Maintained by the `/session-wrap` skill. Newest entries at the top.

---

## Session: 2026-07-11 (token optimization + structured output fix)

**Branch:** `opt/token-reduction-issues-1-2-3`

**Files changed:**
- `src/agentflow/agents/agent.py` — added `_to_dict_content`, `_compact_file_writes`,
  `_successful_write_ids`, `_parse_final_output`; agentic loop stores plain dicts,
  applies file_write compaction after each tool batch, uses new output parser
- `src/agentflow/orchestrator/decomposer.py` — `_DECOMPOSER_TOOLS` reduced to
  `frozenset({"file_read"})`; `bash_exec` removed
- `src/agentflow/orchestrator/engine.py` — removed eager `expand_plan()` call;
  added `_run_micro_subtasks()`; `_dispatch_subtask()` now does lazy decomposition
  with `_skip_decompose` guard
- `tests/test_agent.py` — added 6 `_parse_final_output` unit tests

**Decisions:**
- Decomposer tools restricted to `file_read` only: `bash_exec` caused the decomposer
  to implement the task (create files, run uv commands) rather than analyse it
- Decomposition moved from eager (pre-execution in `engine.run()`) to lazy (at
  dispatch time in `_dispatch_subtask()`) so the decomposer sees the workspace after
  upstream deps complete
- `file_write` tool_use inputs compacted immediately after successful writes to prevent
  file contents accumulating in the LLM cache prefix across subsequent turns — the
  compacted version is what gets cached, so subsequent turns read the stub
- `_parse_final_output` handles prose+fenced-JSON pattern (all three agents in the
  analysed run used this pattern); `output.structured` is now reliably populated and
  `output.text` is prose-only
- Run artifacts live in `.runs/<run_id>/` (dot-prefixed, gitignored), not `runs/`

**Open questions:**
- Issues 4–7 from the run analysis not yet implemented: verbose agent output (4),
  FrontendAgent npm timeout guidance (5), planner exploration heuristic (6), thinking
  budget tuning for impl tasks (7)
- `pyproject.toml` / `uv.lock` gained a workspace member for
  `workspace/markdown-previewer/backend` (written by the CodeAgent during the analysed
  run); these are currently unstaged and should be reviewed before merging the branch

**KB updates applied:**
- `docs/kb/architecture.md` — updated request lifecycle step 4 (lazy decomposition),
  decomposer component entry (read-only tools, lazy invocation, `_run_micro_subtasks`,
  `_skip_decompose`), agent component entry (`_to_dict_content`, `_compact_file_writes`,
  `_parse_final_output`); bumped `last_verified_sha` to `1cf7104`

---

## Session: 2026-07-10 (agent:thought streaming)

**Files changed:**
- `src/agentflow/core/models.py` — added `agent_thought = "agent:thought"` to `SSEEventType`
- `src/agentflow/agents/agent.py` — emit `agent:thought` for text blocks on `tool_use` turns; consolidate `SSEEventType` to module-level import
- `src/agentflow/orchestrator/planner.py` — emit `agent:thought` for text blocks during planner exploration turns

**Decisions:**
- `agent:thought` is emitted only on `tool_use` turns (not `end_turn`) — end-turn text is the final answer, not mid-loop reasoning
- Planner uses `agent_id="planner"` to distinguish its thoughts from subtask agent thoughts
- `SSEEventType` moved from three scattered local imports to one module-level import in `agent.py`

**Open questions:** none

**KB updates applied:**
- `docs/kb/architecture.md` — noted `agent:thought` emission in steps 3 and 6 of the request lifecycle; bumped `last_verified_sha` to `0ee398e`

---

## Session: 2026-07-10 (continued — implementation session)

**Files changed:**
- `src/agentflow/core/context.py` — per-agent message queues (fan-out)
- `src/agentflow/core/context_redis.py` — Redis fan-out via active-agents set + per-agent lists
- `src/agentflow/orchestrator/engine.py` — register/deregister agent lifecycle; strip planner-only context keys from agent TaskContext
- `src/agentflow/agents/agent.py` — `pop_user_message(agent_id)` (agent-scoped)
- `src/agentflow/api/routes.py` — followup: rename `prior_results`→`prior_subtask_outputs`, include `agent_id` in output map
- `src/agentflow/orchestrator/planner.py` — format prior-run context as readable prose section; separate from user-supplied JSON context

**Decisions:**
- Mid-run user messages now fan out to ALL active agents, not just the one that wins the asyncio race — per-agent queues keyed by `agent_id` in both in-process and Redis backends
- `register_agent`/`deregister_agent` lifecycle in `_dispatch_subtask` with `try/finally`; fallback agent explicitly swaps registration before its `run()` call
- Planner-only context keys (`prior_report`, `prior_subtask_outputs`, `prior_run_id`, `prior_task`) stripped from `user_context` before agents receive their `TaskContext` — agents only see what the planner embeds in their `instruction`
- Follow-up context delivered to planner as structured readable section (not raw JSON dump); `prior_subtask_outputs` is a list of `{subtask_id, agent_id, output}` rather than an opaque UUID-keyed dict
- Messages sent during planning phase (before any agent is registered) are silently dropped — accepted trade-off; no agent is listening yet

**Open questions:**
- Messages sent during planning or between subtask dispatches are lost — could buffer them and replay on first `register_agent` if this becomes a user pain point

**KB updates applied:**
- `docs/kb/architecture.md` — updated steps 5 & 6 of request lifecycle; updated `RunContext` entry in data flow section; updated planner component description for follow-up context handling
- `docs/kb/subsystems/redis-backend.md` — replaced `user_messages` key row with `active_agents` + `msg:{agent_id}` rows; added fan-out description to Context & HITL section

---

## Session: 2026-07-10

**Files changed:** `CLAUDE.md`, `.claude/skills/session-wrap/SKILL.md`,
`docs/session-log.md`, `docs/adr/README.md`, `docs/adr/001-redis-state-backend.md`,
`.gitignore`

**Decisions:**
- Expanded `CLAUDE.md` to include build commands, architecture quick-ref, conventions,
  "Do not" rules, and a skills table (aligned with KB patterns research best practices)
- Added `/session-wrap` skill for end-of-session KB maintenance ritual
- Established `docs/adr/` for Architecture Decision Records; seeded with ADR-001 (Redis
  state backend)
- Started `docs/session-log.md` as the breadcrumb trail written by `/session-wrap`
- Added `CLAUDE.local.md` to `.gitignore` so personal overrides are never committed

**Open questions:** none

**KB updates applied:**
- `CLAUDE.md` — expanded from pointer-only doc to full quick-reference (build, arch, conventions, skills)
- `docs/kb/index.md` — no change needed; already references KB correctly
