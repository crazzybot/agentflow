---
title: Redis-Backed State Backend
last_updated: 2026-07-15
last_verified_sha: 17a27d3
sources:
  - src/agentflow/config.py
  - src/agentflow/core/redis_client.py
  - src/agentflow/core/bus.py
  - src/agentflow/core/bus_redis.py
  - src/agentflow/core/context.py
  - src/agentflow/core/context_redis.py
  - src/agentflow/orchestrator/stream.py
  - src/agentflow/orchestrator/stream_redis.py
  - src/agentflow/api/routes.py
  - src/agentflow/main.py
status: current
---

# Redis-Backed State Backend

AgentFlow's per-run state (results/cost/budget, HITL handshake, SSE event stream,
and the dispatch/result bus) can run either **in-process** (the default) or
**backed by Redis**, so multiple API replicas can share one run. The backend is
chosen once at import time from `settings.state_backend`; callers use the same
interfaces either way.

## Selecting a backend

`src/agentflow/config.py` adds three settings (env-overridable via `.env`):

| Setting | Env var | Default | Purpose |
| --- | --- | --- | --- |
| `state_backend` | `STATE_BACKEND` | `memory` | `memory` or `redis`. |
| `redis_url` | `REDIS_URL` | `redis://localhost:6379` | Connection URL. |
| `redis_key_ttl` | `REDIS_KEY_TTL` | `86400` | TTL (s) on every run-scoped key. |
| `redis_max_connections` | `REDIS_MAX_CONNECTIONS` | `10` | Connection pool size passed to `aioredis.from_url`. |

Each of the three module-level singletons is built by a `_make_*()` factory that
returns the Redis variant when `state_backend == "redis"`, otherwise the
in-process default:

- `task_bus` — `_make_task_bus()` in [`core/bus.py`](../../../src/agentflow/core/bus.py)
- `context_store` — `_make_context_store()` in [`core/context.py`](../../../src/agentflow/core/context.py)
- `stream_registry` — `_make_stream_registry()` in [`orchestrator/stream.py`](../../../src/agentflow/orchestrator/stream.py)

All Redis variants share one async client (a single connection pool) from
[`core/redis_client.py`](../../../src/agentflow/core/redis_client.py) via
`get_redis()`; the pool is closed on FastAPI shutdown by the `lifespan` handler
in [`main.py`](../../../src/agentflow/main.py) (`close_redis()`). The client uses
`decode_responses=True`, so all values are plain JSON strings.

## Cross-replica lookups: `get()` vs `connect()`

The base `ContextStore` and `StreamRegistry` gained an async `connect(run_id)`
method alongside the synchronous `get(run_id)`:

- `get()` is a **local-cache-only** lookup — used for fast status checks where a
  miss is acceptable (e.g. the `start_run` emitter poll).
- `connect()` is **Redis-aware** — on a local miss it probes Redis for the run's
  keys and, if present, builds a thin context/emitter bound to the shared client
  so a replica that did not start the run can still stream events or deliver HITL
  input. In the in-process backend `connect()` just delegates to `get()`.

`api/routes.py` calls `connect()` on the SSE stream, HITL input, and run-info
paths so all of them work cross-replica; `list_runs` now builds `RunInfo`s
concurrently with `asyncio.gather`.

## Key layout

All keys are `run:{run_id}:*` and carry `redis_key_ttl`:

| Key | Type | Written by | Purpose |
| --- | --- | --- | --- |
| `…:events` | Stream | `RedisStreamEmitter` | SSE events + a `__done__` sentinel; also the run-existence probe. |
| `…:results` | Hash | `RedisRunContext` | `subtask_id → JSON(AgentResult)`. |
| `…:cost` | String | `RedisRunContext` | Cumulative USD cost (`INCRBYFLOAT`). |
| `…:hitl:pending` | String | `RedisRunContext` | `"1"` while awaiting human input. |
| `…:hitl:queue` | List | `RedisRunContext` | Human response delivery (`RPUSH`/`BLPOP`). |
| `…:active_agents` | Set | `RedisRunContext` | Agent IDs with active subtasks; managed by `register_agent`/`deregister_agent`. |
| `…:msg:{agent_id}` | List | `RedisRunContext` | Per-agent user-message queue; `push_user_message` fans out via `SMEMBERS` then pipelines `RPUSH` to each; `pop_user_message(agent_id)` does `LPOP`. |
| `…:dispatch` | List | `RedisTaskBus` | Orchestrator → worker (not yet wired). |
| `…:result` | List | `RedisTaskBus` | Worker → orchestrator (not yet wired). |

## SSE over Redis Streams

[`stream_redis.py`](../../../src/agentflow/orchestrator/stream_redis.py) mirrors
the in-process `StreamEmitter`/`StreamRegistry` interface. `emit()` stays
synchronous and fires an `XADD` as a `create_task()` (the event loop serialises
those callbacks FIFO, preserving per-run ordering). The `__aiter__` generator
polls with `XREAD BLOCK 1000` starting from id `0`, so reconnecting clients
replay the whole stream; `close()` appends a `__done__` sentinel that terminates
the iterator. `connect()` checks for a trailing sentinel via `XREVRANGE` to mark
already-finished runs done.

## Context & HITL over Redis

[`context_redis.py`](../../../src/agentflow/core/context_redis.py) is
**write-through**: `store_result()` writes to the Redis hash *and* a local dict,
so the synchronous `build_prior_results()`/`build_upstream_artifacts()` helpers work
without an async call, while `get_result()`/`all_results()` fall back to Redis
for cross-replica reads. Cost is tracked both locally (authoritative for
`within_budget()`) and in Redis (`INCRBYFLOAT`); `budget_usd`, `user_context`, and
`task` stay in instance vars and are not persisted to Redis.

HITL delivery is cross-replica safe: `provide_human_input()` (now **async** in
both the base and Redis contexts) runs a Lua script that atomically checks the
`hitl:pending` flag and `RPUSH`es the response only if still pending, so a retry
or a second replica that also seeded `_is_awaiting=True` loses the race and gets
HTTP 409 instead of double-delivering. `await_human_input()` blocks on a
1-second `BLPOP` loop so `asyncio.wait_for` cancels cleanly. Thin contexts from
`connect()` are intentionally **not** cached, to avoid a stale `_is_awaiting`
between two sequential HITL requests on the same run.

Mid-run user message fan-out uses a two-key pattern: `register_agent(agent_id)`
does `SADD run:{id}:active_agents {agent_id}`; `push_user_message(content)`
fetches the set with `SMEMBERS` then pipelines `RPUSH` to each
`run:{id}:msg:{agent_id}` list in one round-trip; `pop_user_message(agent_id)`
does `LPOP` from that agent's list; `deregister_agent(agent_id)` removes the
agent from the set and deletes its list. A message sent while no agents are
registered (e.g. during the planning phase) is silently dropped — agents that
start after the message was sent will not receive it.

## The bus is not yet wired

`RedisTaskBus` ([`bus_redis.py`](../../../src/agentflow/core/bus_redis.py))
implements the dispatch/result list operations, but — like the in-process
`TaskBus` — it is not on the request's critical path today (the orchestrator
drives agents directly via `_dispatch_subtask`). It exists so a future
Celery/worker-pool split can happen without interface changes.

## Running with Redis

```bash
# start a Redis (any 5.x+), then:
STATE_BACKEND=redis REDIS_URL=redis://localhost:6379 \
  uv run uvicorn agentflow.main:app --reload
```

`redis>=5.0.0` is a runtime dependency (`pyproject.toml`).

## Related

- [architecture](../architecture.md) — where these components sit in the run lifecycle.
- [conventions](../conventions.md) — code style and async patterns used in these modules.
