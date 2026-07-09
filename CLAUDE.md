# AgentFlow — Agent Instructions

## Knowledge base (read first)

This repo maintains an agent-facing knowledge base at **`docs/kb/`**.

- **At the start of any non-trivial task:** read [`docs/kb/index.md`](docs/kb/index.md),
  then the KB doc(s) relevant to your change, before exploring source. It exists
  so you don't rediscover the architecture every time.
- The human/design docs in `docs/` (`design.md`, `agentflow_gap_analysis.md`,
  presentation files) are reference material — do not treat them as the KB and do
  not duplicate them.

## Keeping the knowledge base current (required)

**Before you declare any implementation or improvement task complete, run the
`update-kb` skill.** It reconciles `docs/kb/` with your changes and refreshes each
doc's freshness metadata (`last_updated`, `last_verified_sha`). This is not
optional — a task that changed code but left the KB stale is not done.

## Project basics

- Python project managed with `uv`. Run tests with `uv run pytest`.
- Source lives under `src/agentflow/`; see [`docs/kb/codebase-map.md`](docs/kb/codebase-map.md).
