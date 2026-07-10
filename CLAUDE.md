# AgentFlow — Agent Instructions

## Knowledge base (read first)

This repo maintains an agent-facing knowledge base at **`docs/kb/`**.

- **At the start of any non-trivial task:** read [`docs/kb/index.md`](docs/kb/index.md),
  then the KB doc(s) relevant to your change, before exploring source. It exists
  so you don't rediscover the architecture every time.
- The human/design docs in `docs/presentations` (`design.md`, `agentflow_gap_analysis.md`,
  presentation files) are reference material — do not treat them as the KB and do
  not duplicate them.

## Keeping the knowledge base current (required)

**Before you declare any implementation or improvement task complete, run the
`update-kb` skill.** It reconciles `docs/kb/` with your changes and refreshes each
doc's freshness metadata (`last_updated`, `last_verified_sha`). This is not
optional — a task that changed code but left the KB stale is not done.

## Build & test

```bash
uv sync                                          # install / sync deps
uv run uvicorn agentflow.main:app --reload       # API dev server (port 8000)
uv run agentflow run "<task>"                    # CLI entry point
uv run pytest                                    # run the full test suite
uv run pytest tests/ -v                          # verbose
```

- Python `>=3.13`; `uv` is the only supported env/dep manager — no `requirements.txt`.
- Dev DB/state: default is in-process (no external deps). Set `STATE_BACKEND=redis` +
  `REDIS_URL` to enable the Redis backend.
- Config lives in `.env` (gitignored); see `.env.example` for all vars.

## Architecture — quick reference

```
src/agentflow/
  core/          — domain models (Pydantic), RunContext/ContextStore, TaskBus, AgentRegistry, SkillLoader
  agents/        — Agent class: manifest-driven tool-calling loop against the LLM
  orchestrator/  — engine, planner, decomposer, scheduler (DAG), reporter, SSE stream
  llm/           — LLMClient: wraps AsyncAnthropic, injects prompt-cache, tracks UsageStats
  tools/         — tool registry, builtins (bash/file/web/…), MCP adapter, arxiv, artifacts
  api/           — FastAPI routes: /api/runs CRUD, SSE stream, HITL input, cancel, followup, message
  cli/           — terminal client (Rich display of SSE events)
  main.py        — app factory (AgentRegistry + OrchestratorEngine singletons)
  config.py      — Settings (pydantic-settings, loaded from .env)

manifests/       — AgentManifest YAML files, one per agent type
skills/          — per-domain agent skill dirs (SKILL.md + reference docs)
tests/           — flat pytest suite, one file per module
docs/kb/         — agent-facing knowledge base (read before coding)
docs/adr/        — Architecture Decision Records
```

Request flow: `POST /api/runs` → `OrchestratorEngine.run()` → plan → decompose →
schedule DAG → `Agent._agentic_loop()` per subtask → compile report → SSE `run_complete`.

## Conventions

- **Type hints** on all public functions; use `X | None` (not `Optional[X]`), `collections.abc` types, avoid bare `Any`.
- **Data containers**: `@dataclass(frozen=True)` for immutable data; `pydantic.BaseModel` for API/config; `StrEnum` for choices.
- **Logging**: `logging.getLogger(__name__)` — never `print()`.
- **Async**: use `asyncio.gather` for concurrent calls; never `time.sleep` in async code.
- **Tests**: mock LLM/tool calls with `unittest.mock.AsyncMock`; no live API calls in unit tests.
- **Errors**: raise specific types; re-wrap with `raise SomeError(...) from exc`; never swallow silently.

## Do not

- Use `Optional[X]` — use `X | None`.
- Use `print()` — use the logger.
- Touch Pydantic/DB directly in route handlers — route handlers call services/engine only.
- Hard-code credentials or absolute paths.
- Use `time.sleep` in async code.
- Use `except Exception: pass` (swallowing exceptions).
- Commit to `main` directly — always open a PR; CI must pass.

## Available skills

| Skill | Purpose |
|---|---|
| `/update-kb` | Reconcile `docs/kb/` after an implementation task (run before declaring done) |
| `/session-wrap` | End-of-session review: propose CLAUDE.md/KB edits, write session log |

## Source layout

- Source: `src/agentflow/` — architecture quick reference above; deep dive in [`docs/kb/architecture.md`](docs/kb/architecture.md).
- KB docs: `docs/kb/` — see [`docs/kb/index.md`](docs/kb/index.md).
- ADRs: `docs/adr/` — architectural decisions with rationale.
- Session log: `docs/session-log.md` — breadcrumb trail of session decisions.
