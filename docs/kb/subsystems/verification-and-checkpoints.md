---
title: Subtask Verification & Workspace Checkpoints
last_updated: 2026-10-30
last_verified_sha: 2c73ec5
sources:
  - src/agentflow/orchestrator/verifier.py
  - src/agentflow/orchestrator/checkpoints.py
  - src/agentflow/orchestrator/engine.py
  - src/agentflow/orchestrator/planner.py
  - src/agentflow/orchestrator/reporter.py
  - src/agentflow/core/models.py
  - src/agentflow/api/routes.py
status: current
---

# Subtask Verification & Workspace Checkpoints

> **Why it is built this way:** [ADR-006](../../adr/006-independent-verification.md), [ADR-007](../../adr/007-shadow-repo-checkpoints.md).

Two checks run around every subtask in `_dispatch_subtask()`:

- **Verification.** A subtask that has acceptance criteria is not done just because its
  agent says so. An independent read-only verifier must confirm the criteria against the
  workspace.
- **Checkpoints.** The workspace is snapshotted into git after each subtask. A failed
  attempt's files are rolled back before the subtask is tried again.

The order inside a successful attempt is: agent result → (partial continuation) →
`_verify_with_repairs()` → `_with_checkpoint()` → `_store_result()`.

## Acceptance criteria (planner)

- `Subtask.acceptance_criteria: list[str]`. The planner JSON field is
  `acceptanceCriteria`, with 1–5 statements that can be checked by reading files.
- The planner prompt says to omit the field when nothing can be checked objectively,
  and to avoid criteria that need running code, the network, or taste. The verifier is
  read-only and cannot check those.
- `_parse_criteria()` is lenient, like the cost-tuning fields: a non-list or non-string
  value is dropped with a warning (the subtask then runs unverified), blanks are
  stripped, and anything past `_MAX_CRITERIA = 5` is cut.

## Verification (`orchestrator/verifier.py`)

`verify_subtask(run_id, subtask, result, client, emitter) -> (Verification, verifier AgentResult)`:

- **The verifier** is an in-memory manifest: `"{agent}.verifier"`, tools
  `file_read`/`bash_exec_readonly`, `settings.verifier_model_tier` (default `economy`),
  `thinking_effort="low"`, `verifier_max_iterations` (8), and `finalize` on the
  iteration limit.
- **Its input** is the subtask instruction, the criteria, the agent's `files_written`,
  and its reported output capped at 6,000 chars and framed as a claim to verify. It
  never sees the producing agent's conversation.
- **Its verdict** is JSON `{"checks": [{criterion, met, evidence}], "summary"}`, one
  check per criterion in order. `_parse_verdict()` keeps the planner's wording of each
  criterion.
- **`Verification.passed`** is `True` (all met), `False` (any unmet) or `None`
  (inconclusive: the verifier failed, the check count didn't match, or `met` wasn't a
  bool). **Inconclusive passes:** a broken checker never sinks good work, but it is
  logged at warning level.

`OrchestratorEngine._verify_with_repairs()` runs only when `status == success`,
`acceptance_criteria` is non-empty and `ENABLE_VERIFIER` is on. Partial, failed and
fallback results are not verified. **Decomposed subtasks** (agents with a
`decomposition_prompt`, e.g. `CodeAgent`) are verified in `_run_micro_subtasks()`, on
the promoted sink result. That result carries the union of every micro-step's
`files_written`, and repairs continue the sink agent's transcript with an envelope from
`_build_envelope()`. An incomplete result then emits `task:partial`. Each round:

1. Verify, and charge the verifier's cost (`_add_cost`). Record a `subtask_verified`
   session event and emit a `task:verified` SSE event ("passed/failed/inconclusive (n/m
   criteria met)"; the CLI shows it).
2. If the verdict is not `False`, return the result with `verification` set.
3. If `False` and `repairs == VERIFICATION_MAX_REPAIRS` (default 1), downgrade the
   result to **partial** with `error = "Failed independent verification — unmet
   acceptance criteria: …"`. The reporter's "Incomplete Work" section prints
   `Reason: {error}`.
4. Otherwise **repair**: run the same agent again with `resume_messages = result.messages
   + [user: repair_message(verdict)]`, which lists only the unmet criteria and their
   findings. Its cost is charged. A failed or timed-out repair keeps the *earlier*
   output as partial with the repair error. Otherwise the repaired result replaces it
   (`files_written` is the union of both) and the loop verifies again.

## Checkpoints (`orchestrator/checkpoints.py`)

`WorkspaceCheckpoints` is a **shadow repository** at
`<runs_dir>/<run_id>/checkpoints.git` (`GIT_DIR`) with the run's workspace as its
`GIT_WORK_TREE`:

- **It never touches a workspace's own `.git`.** Named workspaces are often user
  projects. Git skips nested `.git` dirs, while the project's `.gitignore` files still
  apply.
- `info/exclude` adds `_DEFAULT_EXCLUDES`: `node_modules/`, `.venv/`, `venv/`,
  `__pycache__/`, `*.pyc`, tool caches, `.tool_output/`, `.DS_Store`.
- **Git runs isolated:** `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, hooks
  disabled, no GPG signing, a fixed AgentFlow identity, and only `PATH` from the
  environment.
- `open()` creates the run dir and the repo if needed. It returns `None` (checkpoints
  off for the run, with a warning) if `git` is missing or init fails. Commit and restore
  errors surface as `CheckpointError`, which the engine logs without failing the run.
- `commit(label)` runs `add --all`. It returns `None` when nothing changed, otherwise a
  `Checkpoint(sha, label, changed)` with `changed` from `diff-tree --root`. All
  operations are serialised by a per-repo `asyncio.Lock`.
- `restore_paths(sha, paths)` restores each path to its state at `sha`, or deletes it if
  it didn't exist there (`sha=None`: no snapshot yet). Paths outside the work tree are
  refused.

Engine integration:

- **Opened** in `_run()` right after the workspace lock is taken (`ENABLE_CHECKPOINTS`,
  default on). It is stored in `self._checkpoints[run_id]` and popped in `finally`. A
  baseline commit is labelled `"run start"` or `"run resumed"`.
- **Per subtask:** `_dispatch_subtask()` records `baseline = HEAD` before its attempts.
  `_with_checkpoint()` commits after a success or partial result (and after a non-failed
  fallback) as `"{id} ({agent}): {status}"`, writes a `checkpoint_created` event
  (`sha`, `label`, `changed`) and stamps `AgentResult.checkpoint`. Micro-subtasks each
  get their own checkpoint.
- **Rollback.** On a failed attempt, before the retry or fallback,
  `_roll_back_attempt()` restores **only that attempt's `files_written`** to `baseline`.
  It records a `workspace_restored` event (`sha`, `paths`) and an `agent:progress` event.
  Parallel subtasks share the workspace, so a full reset would wipe their in-progress
  work.
- `GET /api/runs/{id}/checkpoints` lists `checkpoint_created` events from the session
  log (`RunCheckpointsResponse`), so it needs no git. Manual restore:
  `git --git-dir .runs/<id>/checkpoints.git --work-tree <ws> checkout <sha> -- .`

**Limits:**
- A checkpoint captures the whole workspace when a subtask finishes, so with parallel
  subtasks it can include a sibling's half-written files. Its `changed` list is
  attribution by time, not by agent.
- Rollback only covers files written with `file_write`. Changes a failed attempt made
  through `bash_exec`/`python_exec` stay.
- There is no automatic restore API: a restore while a run is active would race its agents.

## Testing

- `tests/test_verifier.py`: verdict parsing, the verifier's read-only manifest, and the
  engine gate (pass, repair-from-transcript, still failing → partial, inconclusive,
  failed repair, disabled) with `verify_subtask` patched.
- `tests/test_checkpoints.py`: real git in temp dirs (skipped without `git`), covering
  shadow-repo isolation from a project repo, excludes and `.gitignore`, restore
  semantics, and engine checkpoints and rollback via a fake writer agent that fails its
  first attempt.
- Because every engine test now runs `git init` and a commit, setup is slower. Tests
  that need ordering must wait on events, not `sleep` (see
  `test_runs_sharing_a_workspace_are_serialised`).

## Related

- [session-log](session-log.md): `subtask_verified`, `checkpoint_created`, `workspace_restored`.
- [architecture](../architecture.md): dispatch (lifecycle step 5) and the report.
