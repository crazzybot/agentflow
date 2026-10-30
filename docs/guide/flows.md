---
title: Flows
last_updated: 2026-10-29
last_verified_sha: b513b90
sources:
  - src/agentflow/orchestrator/engine.py
  - src/agentflow/orchestrator/planner.py
  - src/agentflow/orchestrator/verifier.py
  - src/agentflow/orchestrator/checkpoints.py
  - src/agentflow/orchestrator/resume.py
  - src/agentflow/orchestrator/campaign.py
  - src/agentflow/orchestrator/workers.py
  - src/agentflow/agents/agent.py
  - src/agentflow/main.py
status: current
---

# Flows

Five things happen in AgentFlow: a run, a subtask inside it, a recovery after a crash, a
campaign session, and an agent call on a worker. Each is described below at the level
you need to reason about behaviour. The [KB](../kb/architecture.md) has the exact details.

## 1. A run, end to end

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant API
    participant E as Engine
    participant P as Planner
    participant A as Agents
    participant R as Reporter
    participant L as Session log

    U->>API: POST /api/runs {task, budget?, workspace?}
    API->>E: start run (background)
    API-->>U: run_id
    U->>API: GET /runs/{id}/stream (SSE)
    E->>L: run_started
    Note over E: claim lease · lock workspace ·<br/>baseline checkpoint
    E->>P: plan the task
    P-->>E: subtask DAG
    E->>L: plan_created
    loop until every subtask is finished
        E->>A: dispatch ready subtasks (in parallel)
        A-->>E: results
        E->>L: results, cost, subtask_finished
    end
    E->>R: compile report
    R-->>E: report.md
    E->>L: run_finished
    E-->>U: run:complete (SSE)
```

- **Planning.** By default the planner always runs. With `DIRECT_AGENT_ID` set, a cheap
  classifier can send simple tasks straight to one agent. Campaign sessions pass a ready
  plan and skip planning.
- **Dependencies.** A subtask receives its dependencies' results (summaries plus the
  files they wrote) in its first message. Large summaries are spilled to a file the agent
  can read.
- **Budget.** The run budget is split across subtasks by the planner's shares. When a
  subtask runs out, the run pauses for human input (`run:awaiting_input`) before spending
  more.

## 2. One subtask: attempt, verify, repair, checkpoint

```mermaid
flowchart TD
    start([subtask ready]) --> decomp{Agent decomposes?}
    decomp -- yes --> micro[Run micro-steps as a mini-DAG<br/>each one goes through this same flow]
    micro --> promote[Promote the final step's result]
    decomp -- no --> attempt[Agent attempt]
    attempt --> ok{Result?}
    ok -- failed --> rollback[Roll back files this attempt wrote] --> retry{Retries left?}
    retry -- yes --> attempt
    retry -- no --> fallback{Fallback agent?}
    fallback -- yes --> fb[Run fallback agent] --> store
    fallback -- no --> failed([failed])
    ok -- partial --> cont[Continue the conversation<br/>or ask a human for budget<br/>up to MAX_CONTINUATIONS] --> ok
    cont -. still partial .-> ckpt
    ok -- success --> crit{Has acceptance<br/>criteria?}
    promote --> crit
    crit -- no --> ckpt
    crit -- yes --> verify[Independent read-only verifier]
    verify -- pass / inconclusive --> ckpt
    verify -- fail --> repair{Repair rounds left?}
    repair -- yes --> fix[Agent continues its own conversation<br/>with the verifier's findings] --> verify
    repair -- no --> partial[Mark partial:<br/>unmet criteria in the report] --> ckpt
    ckpt[Checkpoint the workspace] --> store([store result])
```

**Inside every agent turn:**
- The conversation is shortened for the model (the context view) once it gets long.
- Each tool call passes the tool policies, then runs, in the sandbox if it executes code.
- Every message is appended to the session log.

## 3. Crash and resume

```mermaid
sequenceDiagram
    autonumber
    participant Old as Server (crashes)
    participant L as Session log
    participant New as Server (restarted)
    actor U as User

    Old->>L: …agent turns, results…
    Note over Old: process dies — no run_finished
    New->>L: startup: any run without a terminal event?
    New->>L: run_interrupted (resumable)
    U->>New: POST /runs/{id}/resume (or auto, if enabled)
    New->>L: replay the log
    Note over New: restore results, cost, budget ·<br/>reuse the plan · skip finished subtasks
    New->>New: continue mid-flight subtasks<br/>from their last logged turn
    New->>L: run_resumed … run_finished
```

- **Unanswered tool requests are dropped.** If the agent had asked for tools whose
  results were never logged, that request is removed and the model asks again. It sees the
  workspace as the interrupted tools left it.
- **Multi-replica safety.** With several replicas, a run is only reconciled or resumed if
  no live replica holds its lease.
  [ADR-004](../adr/004-resume-from-log-with-leases.md)

## 4. A campaign session

```mermaid
flowchart TD
    create([POST /api/campaigns]) --> define[Read-only planner writes the feature ledger<br/>ids and checks are fixed from now on]
    define --> loop{Stop?<br/>all passing / max sessions /<br/>budget / only blocked left}
    loop -- yes --> done([completed or stopped])
    loop -- no --> regress[Re-verify a passing feature<br/>regression? → fix it next]
    regress --> pick[Pick the next failing or pending feature]
    pick --> session[Run a session: an ordinary run<br/>fresh context · one feature ·<br/>its checks as acceptance criteria]
    session --> absorb[Read the outcome from the session log]
    absorb --> verdict{Verified pass?}
    verdict -- yes --> passing[feature: passing] --> loop
    verdict -- no --> attempts{Attempts exhausted?}
    attempts -- no --> failing[feature: failing, retried later] --> loop
    attempts -- yes --> blocked[feature: blocked] --> loop
```

- **Session instructions** carry the spec, overall progress, the previous session's own
  notes, and the feature's checks.
- **Pausing** takes effect after the current session. A restart pauses the campaign;
  resuming it finishes the interrupted session first.

## 5. An agent call on a worker

```mermaid
sequenceDiagram
    autonumber
    participant E as Engine (server)
    participant Q as Redis
    participant W as Worker
    participant L as Session log

    E->>Q: queue work item (per-agent queue)
    W->>Q: take item · claim it (heartbeat)
    W->>W: run the agent<br/>(own sandbox, shared workspace)
    W-->>Q: progress events (to the run's SSE stream)
    W->>Q: transcript events, then result
    Q-->>E: events → appended to the log by the engine
    Q-->>E: result
    Note over E,W: unclaimed in time or heartbeat lost →<br/>a failed attempt (normal retry/fallback)<br/>run cancelled → cancel flag → worker stops
```

Only the agent's conversation and tool calls run on the worker. Planning, verification,
checkpoints and the log stay on the server, which is the session log's only writer.
[ADR-010](../adr/010-remote-agent-workers.md)
