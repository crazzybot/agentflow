---
title: Campaigns (Long-Horizon Sessions)
last_updated: 2026-10-29
last_verified_sha: 38d6fc8
sources:
  - src/agentflow/orchestrator/campaign.py
  - src/agentflow/orchestrator/engine.py
  - src/agentflow/core/models.py
  - src/agentflow/main.py
  - src/agentflow/api/routes.py
status: current
---

# Campaigns (Long-Horizon Sessions)

A campaign builds something too large for one run as a **sequence of runs
("sessions") with fresh contexts**, held together by a feature ledger. It follows
the initializer + coding-agent harness pattern, with one deliberate difference: the
ledger is **enforced in code**. Agents never read or write it as a file, and a
feature passes only when the independent verifier confirms it
([verification-and-checkpoints](verification-and-checkpoints.md)).

Code: [`orchestrator/campaign.py`](../../../src/agentflow/orchestrator/campaign.py).
Models (`Feature`, `FeatureStatus`, `Campaign`, `CampaignStatus`, `CampaignRequest`,
`CampaignView`) are in `core/models.py`. `main.py` builds the `campaign_manager`
singleton next to `engine`.

## Storage

The campaign dir is `<runs_dir>/campaigns/<campaign_id>/`. It sits outside every
workspace, so agent tools can't reach it.

- `campaign.json` holds the `Campaign`: `task`, `workspace`, `agent_id`, `max_sessions`,
  budgets, `status`/`stop_reason`, `spent_usd`, `sessions` (run ids), and
  `pending_session`/`pending_feature`.
- `features.json` holds the ledger. `CampaignStore.save_features()` **rejects** any change
  to a feature's `title`/`description`/`checks` and any added or removed feature after
  the first write. Only `status`, `attempts`, `notes`, `evidence`, `last_run_id` and
  `last_checked_session` move. Writes are atomic (temp file + `os.replace`).
- `session.jsonl` is the campaign-level session log. It holds the transcripts of the
  feature planner and the regression verifiers, which run outside any run, via
  `_in_campaign_context()`. That helper also sets `_current_workspace` to the campaign
  workspace.

The workspace is a **named** workspace (`request.workspace`, else `campaign-<id>`), so
every session works on the same tree. Other runs on that name queue behind the
workspace lock.

## The loop (`CampaignManager._loop`)

1. **Define** (only if the ledger is empty; status `defining`). `define_features()` runs
   a read-only agent (`file_read`, `bash_exec_readonly`, `planner_model`, finalize on
   limit). It outputs `{"features": [{title, description, checks}]}`, ordered
   foundations-first. Ids `F01…` are assigned **in code**, checks go through the
   planner's `_parse_criteria()` (1–5), and features without a title or checks are
   dropped. The list is capped at `CAMPAIGN_MAX_FEATURES` (30). An empty result →
   `failed`.
2. **Interrupted session.** If `pending_session` is set, the run is awaited if it's
   active here (startup auto-resume may already have woken it), otherwise `engine.wake()`
   is called on it. `RunNotResumableError` (it already finished) is fine. Then it is
   absorbed.
3. **Each iteration:**
   - **Pause.** If `pause_requested` → `paused`.
   - **Stop check** (`_stop`). All passing → `completed`. Otherwise these stop it
     (`stopped`): only `blocked` features left, `len(sessions) >= max_sessions`, or
     `spent_usd >= budget_usd`.
   - **Regression checks** (`_check_regressions`). The `CAMPAIGN_REGRESSION_CHECKS` (1)
     least recently checked passing features are re-verified with `verify_subtask()`
     against a synthetic claim. Their cost goes to `spent_usd`. A `False` verdict sets
     the feature `failing`, resets `attempts`, and adds a `REGRESSION …` note.
   - **Pick the target** (`_next_feature`): the first `failing`, else the first
     `pending`. `blocked` is skipped.
   - **Record before running.** The new run id is appended to `sessions`, and
     `pending_session`/`pending_feature` are **saved before the run starts**.
   - **Run the session.** `engine.run(run_id, …, workspace=campaign.workspace,
     budget_usd=_session_budget(), plan=…)` with a **preset plan**: one subtask
     `SESSION_SUBTASK_ID = "feature"` for `agent_id`, the instruction from
     `_session_instruction()`, and `acceptance_criteria = feature.checks`.
   - **Absorb** (`_absorb`). It reads the run's **session log** (`replay`), not memory.
     `spent_usd += snapshot.total_cost_usd` (agent and verifier costs; decomposer spend
     isn't counted, as in runs), and `pending_*` is cleared.
     - A `cancelled` run → campaign `paused`.
     - `status == success` **and** `verification.passed is True` → `passing`, with the
       verifier's checks as evidence.
     - Anything else, **including an inconclusive verdict**, → `attempts += 1`, then
       `failing`, or `blocked` at `CAMPAIGN_MAX_FEATURE_ATTEMPTS` (2).
     - The session's final text becomes `feature.notes` (≤2,000 chars).

`_session_instruction()` carries:
- the spec;
- ledger progress (`n/m` and every feature's status);
- the most recent session's notes (`<previous_session_notes>`), standing in for the
  pattern's progress file;
- the target feature, with its checks spelled out;
- for a retry, the attempt count and that feature's last notes;
- the session rules: orient first, this feature only, keep passing features working,
  end with a summary for the next session.

Note that a session's in-run repair rounds (`VERIFICATION_MAX_REPAIRS`) happen
*inside* one session before it is absorbed.

## Operations

| Call | Behaviour |
| --- | --- |
| `POST /api/campaigns` | `create()` validates `agent_id` (unknown → 422) and the workspace name, saves, and `start()`s a background `_drive` task. |
| `GET /api/campaigns[/{id}]` | `CampaignView`: campaign, features, passing/total. |
| `POST …/pause` | Sets `pause_requested`; the campaign stops **after** the current session (409 if not running here). Cancel the session's run to stop sooner. |
| `POST …/resume` | `start()` again; 409 if already active or `completed`/`failed`. A `stopped` campaign may be started again, but it re-checks the stop conditions. |

- **Lease.** `_drive` holds the run lease `campaign-<id>` (`core.lease.run_leases`,
  referenced via the module so tests can patch it) with a keep-alive, so only one replica
  drives a campaign. A replica that can't acquire it logs and returns. Loop errors pause
  the campaign with `stop_reason="error: …"`. Task cancellation pauses it too.
- **Restart.** The lifespan calls `await campaign_manager.mark_interrupted()` after run
  reconciliation and run auto-resume. `running`/`defining` campaigns not leased
  elsewhere → `paused` ("service restarted"). With `RESUME_INTERRUPTED_RUNS=true` they
  are `start()`ed again.

## Engine hooks added for campaigns

- `engine.run(..., plan=ExecutionPlan)` skips the classifier and planner. The
  `plan_created` session event carries `preset: true` and the SSE message reads "Preset
  plan".
- `engine.wait_for(run_id)` waits for a run active in this process.
- `engine.client` exposes the shared `LLMClient`.

## Testing

`tests/test_campaign.py` covers:
- ledger immutability and feature definition, with the planner `Agent` patched;
- instruction content and policy;
- full loops through the real engine, with a fake coder agent and `verify_subtask`
  patched separately in `engine` (session verdicts) and `campaign` (regression
  verdicts): completion, blocking, inconclusive-is-not-pass, session and budget stops,
  regression reopening, failed definition, graceful pause, absorbing a session that
  finished before a crash, restart pause, and the routes.

The fixture sets `verification_max_repairs=0`, so each verdict is one session outcome.

## Related

- [verification-and-checkpoints](verification-and-checkpoints.md) — how a session's feature is verified.
- [resume-and-leases](resume-and-leases.md) — `wake()` and leases reused here.
- [session-log](session-log.md) — what `_absorb` reads.
