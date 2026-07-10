# ADR-001: Redis state backend for multi-replica runs

**Status:** Accepted
**Date:** 2026-07-09

## Context

AgentFlow's default execution model is entirely in-process: `TaskBus`, `ContextStore`,
and `StreamRegistry` are asyncio-based singletons that live in a single Python process.
This is zero-dependency and easy to run locally, but it means all replicas of the API
service are isolated — a run started on replica A cannot be monitored or have HITL input
delivered from replica B.

As the service scales (multiple uvicorn workers, Kubernetes pods, or container replicas),
we needed a shared state layer for:
1. Cross-replica SSE streaming (`GET /api/runs/{id}/stream`)
2. Cross-replica HITL delivery (`POST /api/runs/{id}/input`)
3. Shared run context (subtask results, cost tracking, budget arbitration)

## Decision

Add an optional Redis-backed implementation for all three stateful components, selected
at startup by setting `STATE_BACKEND=redis` in the environment:

- `TaskBus` → `RedisTaskBus` (`core/bus_redis.py`)
- `ContextStore` → `RedisContextStore` (`core/context_redis.py`)
- `StreamRegistry` → `RedisStreamRegistry` (`orchestrator/stream_redis.py`)

Each component has a factory function (`_make_task_bus()`, `_make_context_store()`,
`_make_stream_registry()`) that returns the appropriate implementation based on
`settings.state_backend`. The in-process default requires no change to the existing API.

Redis Streams (`XADD`/`XREAD`) are used for SSE events so replicas can tail a run's
event log from any point. Key TTLs are controlled via `REDIS_KEY_TTL` (default 24 h).

## Consequences

- **Positive:** Multi-replica deployments work without a sticky-session load balancer.
  HITL and streaming work correctly across replicas.
- **Positive:** The `redis>=5.0.0` dependency is already in `pyproject.toml`; no new
  third-party package needed.
- **Negative:** Local development now requires `docker compose up redis` (or equivalent)
  when `STATE_BACKEND=redis` is set. The default (in-process) still needs nothing.
- **Negative:** Added `*_redis.py` modules increase surface area for state-management bugs.
  The factory pattern keeps this isolated, but it must be kept in sync with the in-process
  equivalents.

## Alternatives considered

- **Sticky sessions (load-balancer level):** Avoids Redis entirely but constrains
  deployment topology and prevents failover between replicas mid-run. Rejected.
- **PostgreSQL LISTEN/NOTIFY for events:** Familiar but lacks the native stream-replay
  semantics of Redis Streams and would require Postgres as an additional dependency.
  Rejected.
- **Celery / external task queue:** Would require a full rewrite of the scheduling layer.
  Deferred to a future ADR if worker-pool separation becomes necessary.
