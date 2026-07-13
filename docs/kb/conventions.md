---
title: Conventions & Patterns
last_updated: 2026-07-13
last_verified_sha: 5a2832d
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
- Run the app: `uv run uvicorn agentflow.main:app --reload` (API) or
  `uv run agentflow run "<task>"` (CLI). The console script `agentflow` maps to
  `agentflow.cli:main` (see `[project.scripts]`).
- Run tests: `uv run pytest` (or `uv run pytest tests/ -v`).
- Dev-only deps (`pytest`, `pytest-asyncio`) live in `[dependency-groups].dev`, separate
  from runtime `dependencies`.
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
  claude-sonnet-4-6 rates and can be overridden in `.env`.
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
  module under test: `test_agent.py`, `test_scheduler.py`, `test_models.py`,
  `test_registry.py`, `test_skill_loader.py`, `test_tools.py`, `test_arxiv_search.py`,
  `test_stream.py`.
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
