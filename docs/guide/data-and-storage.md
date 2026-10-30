---
title: Data & storage
last_updated: 2026-10-30
last_verified_sha: 372c48d
sources:
  - src/agentflow/config.py
  - src/agentflow/core/session.py
  - src/agentflow/core/workspace.py
  - src/agentflow/orchestrator/engine.py
  - src/agentflow/orchestrator/checkpoints.py
  - src/agentflow/orchestrator/campaign.py
  - src/agentflow/orchestrator/workers.py
  - src/agentflow/core/lease.py
  - src/agentflow/core/context_redis.py
  - src/agentflow/orchestrator/stream_redis.py
status: current
---

# Data & storage

AgentFlow keeps no database. Durable state is files under `.runs/` (`RUNS_DIR`) and the
workspaces (`WORKSPACE_DIR`). Redis, when enabled, holds only *live* coordination state,
which expires.

## On disk

```text
.runs/
├── <run_id>/
│   ├── meta.json          task, generated name, created_at, workspace
│   ├── session.jsonl      ★ the durable record: every event and every agent conversation
│   ├── report.md          final report (or an "interrupted" placeholder until resumed)
│   ├── artifacts.jsonl    files the agents wrote (deliverables)
│   ├── checkpoints.git/   shadow git repo of the workspace (one commit per subtask)
│   ├── events.jsonl       copy of the SSE stream            (only with CAPTURE_EVENTS)
│   └── results.jsonl      subtask results                   (only with CAPTURE_RESULTS)
└── campaigns/<campaign_id>/
    ├── campaign.json      status, budget/spend, sessions (run ids), pending session
    ├── features.json      ★ the feature ledger (definitions fixed after creation)
    └── session.jsonl      transcripts of the feature planner and regression checks

workspace/
├── runs/<run_id>/         fresh per-run workspace (default)
├── <name>/                named project workspace (e.g. app-gateway/), shared by runs
└── semantic-kb/           the knowledge-base MCP server's project
```

- **★ marks a source of truth.** A restart rebuilds a run from `session.jsonl` and a
  campaign from its two JSON files.
- **Agents can't reach `.runs/`.** Their tools are confined to the workspace, which is
  how the campaign ledger is protected from them.
- **Treat both trees as sensitive.** The session log contains full prompts and tool
  output (anything an agent read), and the workspace contains whatever the agents fetched.

### The session log, briefly

Each line is one event with a per-run sequence number:

- **Run level:** `run_started`, `plan_created`, `run_interrupted`, `run_resumed`,
  `run_finished`.
- **Subtask level:** `subtask_dispatched`, `subtask_decomposed`, `subtask_finished`,
  `result_stored`, `cost_added`, `subtask_verified`.
- **Agent level:** `agent_started` / `agent_finished`, and `message_appended` for every
  turn of every invocation.
- **Workspace:** `checkpoint_created`, `workspace_restored`.
- **Human input:** `human_input_requested` / `_received`.

`GET /api/runs/{id}/session` returns slices of it, even while the run is live. The full
event list is in the [KB](../kb/subsystems/session-log.md).

## In Redis (only with `STATE_BACKEND=redis`)

| What | Keys | Lifetime |
| --- | --- | --- |
| Live SSE stream | `run:<id>:events` (Redis Stream) | `REDIS_KEY_TTL` (24 h) |
| Run context: results, cost, pending human input, per-agent message queues | `run:<id>:results`, `:cost`, `:hitl:*`, `:active_agents`, `:msg:*` | `REDIS_KEY_TTL` |
| Ownership leases and cancel requests | `run:<id>:lease`, `run:<id>:cancel` (campaigns use `run:campaign-<id>:…`) | lease: `RUN_LEASE_TTL_S` (15 s), renewed while alive |
| Worker queues and in-flight items | `agentflow:work:<agent_id>`, `work:<item>:{claimed,events,result,cancel}` | until consumed / `REDIS_KEY_TTL` |

Everything in Redis can be lost without losing a run. It only coordinates replicas and
workers *while* runs execute.

## What survives what

| Event | Effect |
| --- | --- |
| API process restarts | Live runs are marked interrupted, and you can resume them. Campaigns pause. Nothing on disk is lost. |
| Redis is flushed | Live streams and human-input handshakes in progress are lost. Durable records are unaffected. |
| A worker dies | That subtask attempt fails and is retried or handed to a fallback agent. |
| `.runs/` is deleted | History, resume and campaigns are gone. Workspaces remain. |
