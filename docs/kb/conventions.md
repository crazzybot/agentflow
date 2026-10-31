---
title: Conventions & Patterns
last_updated: 2026-10-30
last_verified_sha: 564a6a1
sources:
  - pyproject.toml
  - src/agentflow/config.py
  - src/agentflow/logging_config.py
  - tests/
  - skills/python-coding/
status: current
---

# Conventions & Patterns

## Tooling

- Dependency/env management is `uv`; `pyproject.toml` is the single source of metadata
  and dependencies (no `requirements.txt`).
- Python version: `>=3.13` (`requires-python` in `pyproject.toml`).
- Install/sync: `uv sync`.
- Run the app: `uv run uvicorn agentflow.main:app --reload --port 8002` (API) or
  `uv run agentflow run "<task>"` (CLI). The console script `agentflow` maps to
  `agentflow.cli:main` (see `[project.scripts]`).
- Run tests: `uv run pytest` (or `uv run pytest tests/ -v`). `pyproject.toml` sets
  `testpaths = ["tests"]` so projects that agents generate in a workspace (which ship their
  own test suites) are never collected if `WORKSPACE_DIR` points inside the repo.
- Dev-only deps (`pytest`, `pytest-asyncio`, `fakeredis[lua]`) live in
  `[dependency-groups].dev`, separate from runtime `dependencies`. `fakeredis[lua]`
  backs the lease tests, which need real Lua `EVAL` semantics.
- Runtime `dependencies` include `redis>=5.0.0`, used only when the optional Redis state
  backend is enabled (`STATE_BACKEND=redis`); the in-memory default needs no Redis server.
  New settings follow the existing `Settings` pattern — added as typed fields with
  defaults and overridden via `.env`/env vars (`STATE_BACKEND`, `REDIS_URL`,
  `REDIS_KEY_TTL`). See [subsystems/redis-backend](subsystems/redis-backend.md).

## Code style

Summarized from `skills/python-coding/SKILL.md` + `best_practices.md` — the project's
own stated conventions (not all patterns below are exercised in every file, but none
contradict the source):

- **Type hints everywhere** on public functions/attributes; prefer `collections.abc`
  types and `X | Y` / `X | None` union syntax over `typing.Union`/`Optional`. Avoid bare
  `Any`.
- **Data containers**: `@dataclass(frozen=True)` for immutable data, `@dataclass` for
  mutable internal structs, `pydantic.BaseModel` for API/config/validated input,
  `enum.Enum`/`StrEnum` for choices. `src/agentflow/config.py` follows the config case:
  `Settings(BaseSettings)` from `pydantic_settings`, loaded from `.env` via
  `SettingsConfigDict(env_file=".env", ...)`, instantiated once as a module-level
  `settings = Settings()` singleton. Pricing fields (`cost_per_1m_input_tokens`,
  `cost_per_1m_output_tokens`, `cost_per_1m_cache_write_tokens`,
  `cost_per_1m_cache_read_tokens`, `cost_per_1m_thinking_tokens`) default to
  claude-opus-5-5's rate ($4/$20 per 1M, matching `agent_model`'s default) and can be
  overridden in `.env`; they
  are only the fallback rate for a model not listed in `agents/agent.py`'s
  `_MODEL_PRICING` table (a warning is logged once per such model). When a new model
  ships, add it to `_MODEL_PRICING` (or `_LONG_PROMPT_PRICING` for prompt-length-tiered
  models like Haiku 5.5), and to `_ADAPTIVE_THINKING_PREFIXES` in
  `llm/client.py` if it takes adaptive thinking/effort, rather than editing these defaults.
  Named cost tiers (`model_tier_economy`/`_standard`/`_premium`) follow the same pattern —
  typed `Settings` fields resolved by `settings.resolve_model_tier(tier)` — so manifests
  and planner-assigned per-subtask overrides can reference `"economy"`/`"standard"`/
  `"premium"` instead of a raw model id; see [Architecture](architecture.md)'s `Agent`
  cost-tiering precedence.
- **Error handling**: raise specific, narrow exception types; never swallow exceptions
  silently; add context with `raise SomeError(...) from exc` when re-wrapping.
- **Logging**: standard `logging` module, never `print()`. `logging_config.py` centralizes
  setup in `setup_logging(level, json_format, log_file)`, called once at the application
  entry point — it configures the root logger, clears existing handlers, adds a console
  (and optional file) handler, and quiets noisy third-party loggers. Modules get a logger
  via `get_logger(__name__)` / `logging.getLogger(__name__)`.
- **Project layout**: `src/` layout (`src/agentflow/`), one top-level package, tests in a
  separate top-level `tests/` directory.
- Common anti-patterns called out in `best_practices.md` to avoid: `except Exception: pass`,
  mutable default arguments, `import *`, hard-coded credentials/paths, `time.sleep` in
  async code, `str` for structured data.

## Async

- `pyproject.toml` sets `[tool.pytest.ini_options] asyncio_mode = "auto"`, and
  `pytest-asyncio` is a dev dependency, so async test functions are collected and run
  without extra config (tests in this repo still mark them explicitly with
  `@pytest.mark.asyncio`, e.g. `tests/test_agent.py`).
- Orchestrator and agent modules are predominantly async: `OrchestratorEngine.run`,
  `create_plan`, `expand_plan`, `compile_report` (in `src/agentflow/orchestrator/`) and
  `Agent.run` / `Agent._agentic_loop` (in `src/agentflow/agents/agent.py`) are all
  `async def`.
- Concurrent calls use `asyncio.gather` (e.g. tool-result gathering in
  `src/agentflow/agents/agent.py` and `src/agentflow/orchestrator/planner.py`) rather than
  manual task bookkeeping.

## Testing

- Tests live in the top-level `tests/` directory (flat, no subpackages), one file per
  module under test: `test_agent.py`, `test_decomposer.py`, `test_scheduler.py`,
  `test_models.py`, `test_registry.py`, `test_skill_loader.py`, `test_tools.py`,
  `test_arxiv_search.py`, `test_stream.py`, `test_planner.py`, `test_config.py`,
  `test_reporter.py`, `test_engine.py` (run routing with `create_plan` patched and the
  engine's own LLM helpers replaced by `AsyncMock`), `test_workspace.py` (workspace
  resolution, tool isolation, locking, followup inheritance), `test_session.py` (session
  log append/read/replay, agent transcripts, engine replay parity), `test_verifier.py`
  (verdicts and the engine's verify/repair gate), `test_checkpoints.py` (real git in temp
  dirs, skipped without git), `test_sandbox.py` (local and fake-docker sandboxes, per-run
  lifetime), `test_policy.py` (shell parsing, allowlists, limits), `test_workers.py`
  (remote execution on fakeredis with a real worker and engine), `test_campaign.py` (ledger, definition, and the session loop
  through the real engine), `test_context_view.py`
  (read-time snipping and recall), `test_resume.py`
  (crash → reconcile → `wake()`), `test_lease.py` (Redis leases on `fakeredis`,
  two engines as two replicas), and `test_context.py`
  (behaviour that must match across the memory and Redis backends, parametrized over both).
- An autouse fixture in `tests/conftest.py` (`_isolated_data_dirs`) points
  `settings.workspace_dir` and `settings.runs_dir` at a fresh temp home for every test,
  because the defaults are in the user's home (`~/agentflow/`). Tests that need a known
  dir monkeypatch it again (e.g. `ws_root` in `test_workspace.py`, `tmp_path` runs dirs
  in engine tests). The global
  `stream_registry`/`context_store`/`task_bus` follow the developer's `.env`. With
  `STATE_BACKEND=redis` and no Redis server they log connection errors, and they are
  `Redis*` classes without the in-memory internals. A test that inspects emitted events
  should monkeypatch `agentflow.orchestrator.engine.{stream_registry,context_store,task_bus}`
  with in-memory instances (see the `engine` fixture in `tests/test_workspace.py`).
- `tests/conftest.py` has one autouse fixture, which forces both
  `agentflow.orchestrator.engine.run_leases` and `agentflow.core.lease.run_leases`
  (used by campaigns) to the in-process `RunLeases`. Lease calls are awaited, not fire-and-forget, so a
  Redis `.env` without a server would fail every engine test. Lease tests opt back in
  by monkeypatching `agentflow.orchestrator.engine.run_leases` with a `RedisRunLeases`
  on `fakeredis`.
- Never order concurrent engine work with `asyncio.sleep(<small>)`: run setup now
  includes git subprocesses (checkpoints), so timing guesses break. Wait on an
  `asyncio.Event` set by the mocked step, or poll for an emitted event.
- To simulate a crash, run the engine, block a subtask, cancel, then strip the trailing
  `run_finished` line from `session.jsonl` (`_simulate_crash` in `tests/test_resume.py`).
  A killed process never reaches the `finally` that writes it.
- Naming: files are `test_*.py`; test functions are `test_<behavior>` (e.g.
  `test_agent_run_end_turn`, `test_dependency_blocks_downstream`).
- Structure seen in `tests/test_agent.py` and `tests/test_scheduler.py`: small `_make_*`/`_plan`
  helper factories build fixtures (manifests, envelopes, plans) inline rather than via
  `pytest.fixture`; LLM/tool calls are mocked with `unittest.mock.MagicMock`/`AsyncMock`
  (no live LLM or MCP calls in unit tests).
- Async test pattern: `async def test_...()` decorated with `@pytest.mark.asyncio`,
  awaiting the code under test directly (see every test in `tests/test_agent.py`).
- Assertions target both success paths and error/edge cases in the same file (e.g.
  `test_cycle_detection` asserting `pytest.raises(ValueError, match="cycle")` in
  `tests/test_scheduler.py`).

## Related

- [Architecture](architecture.md) — how these modules fit together at runtime.
- [`/how-to` skill](../../.claude/skills/how-to/SKILL.md) — step-by-step recipes for adding agents, tools, and skills.
