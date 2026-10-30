---
title: Deployment modes
last_updated: 2026-10-30
last_verified_sha: 372c48d
sources:
  - src/agentflow/config.py
  - src/agentflow/main.py
  - src/agentflow/cli/__init__.py
  - src/agentflow/tools/sandbox.py
  - src/agentflow/orchestrator/workers.py
  - src/agentflow/core/lease.py
status: current
---

# Deployment modes

Four independent settings determine how AgentFlow runs. The defaults give a
single process with no external dependencies.

| Setting | Values | Default | What it changes |
| --- | --- | --- | --- |
| `STATE_BACKEND` | `memory`, `redis` | `memory` | Where *live* run state lives (streams, context, leases) |
| `DISPATCH_MODE` | `local`, `workers` | `local` | Where agent conversations execute |
| `SANDBOX_BACKEND` | `local`, `docker` | `local` | Where agent-issued shell/Python commands run |
| `RESUME_INTERRUPTED_RUNS` | `true`, `false` | `false` | Whether a restart resumes interrupted runs and campaigns automatically |

## Common setups

### 1. Single process (development, default)

```bash
uv run uvicorn agentflow.main:app --reload
uv run agentflow run "<task>"
```

Everything runs in one Python process: no Redis, no Docker. Runs survive a restart and
can be resumed with `POST /api/runs/{id}/resume`.

### 2. Several API replicas

```bash
STATE_BACKEND=redis REDIS_URL=redis://… uvicorn agentflow.main:app   # × N, behind a load balancer
```

- **Any replica can serve any run's stream, human input, messages and cancel.** Leases
  stop replicas from interfering with each other's runs.
- **Replicas must share `.runs/` and the workspace root** (for example a shared volume).
- **Known gap:** an interrupted run is only noticed when some replica *starts up*.

### 3. Server plus workers

```bash
DISPATCH_MODE=workers STATE_BACKEND=redis uvicorn agentflow.main:app
uv run agentflow worker                               # serve every agent
uv run agentflow worker --agents CodeAgent --concurrency 4
```

- **The server plans and checks; workers execute.** Agent conversations and their tool
  calls run on the workers, while planning, verification, checkpoints and the session log
  stay on the server.
- **Specialised hosts.** Each worker serves the agents it's started with, so different
  hosts can carry different toolchains or sandboxes.
- **Shared filesystem required.** Workers need the same `.runs/` and workspace root as
  the server.
- **Failures become retries.** A worker that dies, or a task nobody picks up within
  `WORKER_CLAIM_TIMEOUT_S`, becomes a failed attempt that is retried.

### 4. Docker sandbox (combines with any of the above)

```bash
SANDBOX_BACKEND=docker SANDBOX_DOCKER_NETWORK=none   # or bridge (default) for package installs
```

- **Per-run container.** `bash_exec`, `bash_exec_readonly` and `python_exec` run in a
  container started on a run's first command. The workspace is mounted at `/workspace`;
  there are no capabilities, no host credentials, CPU/memory/process limits, and your uid
  instead of root.
- **Fails closed.** If Docker is missing, these tools fail; they never quietly run on the
  host.
- **With workers,** each worker uses its own Docker on its own host.

## Settings worth knowing

**Models and cost**
- `PLANNER_MODEL`, `AGENT_MODEL`, and `MODEL_TIER_ECONOMY/STANDARD/PREMIUM`, which
  re-tier every agent at once.
- `AGENT_THINKING_EFFORT` and `REPORT_MODEL_TIER`.

**Limits**
- `TASK_TIMEOUT_MS`, `TASK_MAX_RETRIES`, `MAX_CONTINUATIONS`, `AGENT_MAX_ITERATIONS`.

**Quality gates**
- `ENABLE_VERIFIER` and `VERIFICATION_MAX_REPAIRS` (independent checks).
- `ENABLE_CHECKPOINTS` (git snapshots).
- `ENABLE_DECOMPOSER` (micro-steps).

**Context**
- `CONTEXT_SNIP_TRIGGER_TOKENS`: when long conversations start being shortened for the
  model; `0` turns it off.

**Campaigns**
- `CAMPAIGN_MAX_FEATURES`, `CAMPAIGN_MAX_FEATURE_ATTEMPTS`, `CAMPAIGN_REGRESSION_CHECKS`.

**Safety**
- `SANDBOX_ENV_PASSTHROUGH`: environment variables you explicitly allow into agent
  commands.
- The manifest's `tool_policies`: per-agent command allowlists.

All settings, with defaults, are in `.env.example` and the README's configuration table.
