# Agent Knowledge Base Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a parallel, agent-facing knowledge base of interlinked markdown docs under `docs/kb/`, plus the rules and self-review skill that keep it current after every task.

**Architecture:** A `docs/kb/` tree with a hub `index.md` and five core docs, each carrying YAML frontmatter (`last_updated`, `last_verified_sha`, `sources`, `status`) that powers mechanical drift detection. A root `CLAUDE.md` makes agents discover and read the KB, and a `.claude/skills/update-kb/` project skill gives them the end-of-task reconciliation checklist. Existing long-form docs are untouched.

**Tech Stack:** Markdown + YAML frontmatter, git (`git log` for drift detection), Claude Code project skills. No new runtime dependencies.

## Global Constraints

- KB lives under `docs/kb/`; existing docs (`docs/design.md`, `README.md`, `docs/agentflow_gap_analysis.md`, presentation files) are NEVER modified.
- Every KB doc opens with frontmatter fields, in this order: `title`, `last_updated`, `last_verified_sha`, `sources`, `status`.
- `last_updated` uses ISO date `YYYY-MM-DD`. Today is `2026-07-09`.
- `last_verified_sha` = the short HEAD SHA at the moment the doc is authored/committed (`git rev-parse --short HEAD`). Because doc commits never touch `src/`, this keeps the drift query empty at seed time.
- `status` is one of `current | stale | draft`. Seed all core docs as `current`.
- Interlink docs with relative markdown links (`[concepts](concepts.md)`); anchor into code with relative links (`[orchestrator](../../src/agentflow/orchestrator/)`).
- Every path listed in a doc's `sources:` MUST exist in the repo.
- Prose is agent-optimized: facts, invariants, and pointers — not narrative. Keep each core doc under ~250 lines.

## Reference: current codebase inventory (source of truth for seeding)

```
src/agentflow/
  __init__.py  __main__.py  main.py  config.py  logging_config.py
  agents/        agent.py
  api/           routes.py
  cli/           client.py  display.py
  core/          bus.py  context.py  models.py  registry.py  skill_loader.py
  llm/           client.py
  orchestrator/  decomposer.py  engine.py  planner.py  reporter.py  scheduler.py  stream.py
  tools/         artifact_tracker.py  arxiv_search.py  builtin.py  mcp_tools.py  registry.py  skills.py
manifests/       *_agent.yaml (business_analyst, code, data, financial_analyst, frontend, knowledgebase, planner, research, writer)
skills/          business-analysis  equity-research  financial-analysis  frontend-web  python-coding  python-data-analysis  technical-analysis
tests/           test_agent.py test_arxiv_search.py test_models.py test_registry.py test_scheduler.py test_skill_loader.py test_tools.py
```

Tests run with `pytest` (config in `pyproject.toml`, `pytest-asyncio` present). Project uses `uv`.

## Reusable verification snippet (used by several tasks)

Run from repo root after authoring a doc. Replace `DOC` with the file path:

```bash
DOC=docs/kb/architecture.md
# 1. Frontmatter present and parses (requires PyYAML, already available via uv)
uv run python -c "
import sys, yaml
raw = open('$DOC').read()
assert raw.startswith('---'), 'no frontmatter'
fm = yaml.safe_load(raw.split('---',2)[1])
for k in ['title','last_updated','last_verified_sha','sources','status']:
    assert k in fm, f'missing frontmatter key: {k}'
assert fm['status'] in ('current','stale','draft'), 'bad status'
print('frontmatter OK:', fm['title'])
# 2. Every source path exists
import os
for p in fm['sources']:
    assert os.path.exists(p), f'source path missing: {p}'
print('sources OK:', fm['sources'])
"
# 3. Drift query is empty at seed time (SHA == HEAD, so no diff)
SHA=$(uv run python -c "import yaml;print(yaml.safe_load(open('$DOC').read().split('---',2)[1])['last_verified_sha'])")
echo "drift check (expect empty):"; git log --oneline ${SHA}..HEAD -- $(uv run python -c "import yaml;print(' '.join(yaml.safe_load(open('$DOC').read().split('---',2)[1])['sources']))")
```

Expected: prints `frontmatter OK`, `sources OK`, and an empty drift check.

---

### Task 1: Scaffold KB tree and author the hub `index.md`

**Files:**
- Create: `docs/kb/index.md`
- Create: `docs/kb/subsystems/.gitkeep`

**Interfaces:**
- Produces: the `docs/kb/` directory, the frontmatter convention (concrete example other docs copy), and the hub that links to all five core docs (`architecture.md`, `codebase-map.md`, `concepts.md`, `conventions.md`, `how-to.md`).

- [ ] **Step 1: Create the empty subsystems growth-valve directory**

```bash
mkdir -p docs/kb/subsystems
touch docs/kb/subsystems/.gitkeep
```

- [ ] **Step 2: Author `docs/kb/index.md`**

Use this exact frontmatter (stamp `last_verified_sha` from `git rev-parse --short HEAD`):

```markdown
---
title: AgentFlow Knowledge Base — Index
last_updated: 2026-07-09
last_verified_sha: <short HEAD sha>
sources:
  - src/agentflow/
  - manifests/
  - skills/
status: current
---

# AgentFlow Knowledge Base

Agent-facing knowledge base. **Read this first on any non-trivial task**, then
the doc(s) relevant to your change, before exploring source.

> These docs are maintained separately from the human/design docs in `docs/`
> (`design.md`, `../agentflow_gap_analysis.md`, presentation material). Do not
> duplicate those; link to them only when needed.

## Reading order

1. [Architecture](architecture.md) — how AgentFlow works end-to-end.
2. [Codebase map](codebase-map.md) — where everything lives.
3. [Concepts](concepts.md) — domain glossary.
4. [Conventions](conventions.md) — patterns, structure, testing.
5. [How-to](how-to.md) — task recipes.

## Subsystem deep-dives

`subsystems/` holds per-subsystem docs added as the system grows. (Empty today.)

## Keeping this KB current

After any implementation/improvement task, run the `update-kb` skill (see
`CLAUDE.md`). Each doc's frontmatter carries `last_verified_sha` + `sources`;
drift is detected by `git log <last_verified_sha>..HEAD -- <sources>`.
```

- [ ] **Step 3: Verify the hub**

Run the reusable verification snippet with `DOC=docs/kb/index.md`.
Expected: `frontmatter OK`, `sources OK`, empty drift check.

- [ ] **Step 4: Verify the linked targets are named consistently**

Run: `grep -oE '\]\([a-z-]+\.md\)' docs/kb/index.md | sort -u`
Expected: exactly `](architecture.md)`, `](codebase-map.md)`, `](concepts.md)`, `](conventions.md)`, `](how-to.md)`. (Targets are created in later tasks; names must match.)

- [ ] **Step 5: Commit**

```bash
git add docs/kb/index.md docs/kb/subsystems/.gitkeep
git commit -m "docs(kb): scaffold knowledge base tree and hub index"
```

---

### Task 2: Author `architecture.md`

**Files:**
- Create: `docs/kb/architecture.md`

**Interfaces:**
- Consumes: frontmatter convention from Task 1.
- Produces: the end-to-end system narrative other docs link to as `[architecture](architecture.md)`.

- [ ] **Step 1: Read the sources before writing**

Read `src/agentflow/main.py`, `src/agentflow/orchestrator/engine.py`, `decomposer.py`, `planner.py`, `scheduler.py`, `reporter.py`, `stream.py`, `src/agentflow/agents/agent.py`, `src/agentflow/core/bus.py`, `src/agentflow/core/context.py`, `src/agentflow/llm/client.py`. Trace one request from entry to result.

- [ ] **Step 2: Author `docs/kb/architecture.md`**

Frontmatter (stamp `last_verified_sha` from `git rev-parse --short HEAD`):

```markdown
---
title: Architecture Overview
last_updated: 2026-07-09
last_verified_sha: <short HEAD sha>
sources:
  - src/agentflow/main.py
  - src/agentflow/orchestrator/
  - src/agentflow/agents/agent.py
  - src/agentflow/core/bus.py
  - src/agentflow/core/context.py
  - src/agentflow/llm/client.py
status: current
---
```

Required sections (fill from what you read — real component and function names, no placeholders):
- `# Architecture Overview` — one-paragraph what-and-why.
- `## Request lifecycle` — ordered steps from entry (`main.py` / API) through orchestration (decompose → plan → schedule → execute) to reporting. Name the actual modules/classes at each step.
- `## Components` — the orchestrator pieces (`decomposer`, `planner`, `scheduler`, `engine`, `reporter`, `stream`), agents, the message `bus`, shared `context`, and the LLM client — one or two lines each on responsibility.
- `## Data flow & messaging` — how components communicate (`core/bus.py`), what shared state lives in `core/context.py`.
- `## Related` — links to [codebase-map](codebase-map.md), [concepts](concepts.md).

- [ ] **Step 3: Verify**

Run the reusable verification snippet with `DOC=docs/kb/architecture.md`.
Expected: `frontmatter OK`, `sources OK`, empty drift check.

- [ ] **Step 4: Verify internal links resolve**

Run: `for f in codebase-map concepts; do test -f docs/kb/$f.md || echo "MISSING (created later, OK if pending): $f"; done`
Note: targets may not exist yet if tasks run out of order — that is acceptable; final validation happens in Task 8.

- [ ] **Step 5: Commit**

```bash
git add docs/kb/architecture.md
git commit -m "docs(kb): add architecture overview"
```

---

### Task 3: Author `codebase-map.md`

**Files:**
- Create: `docs/kb/codebase-map.md`

**Interfaces:**
- Consumes: frontmatter convention from Task 1.
- Produces: the "where things live" reference linked as `[codebase-map](codebase-map.md)`.

- [ ] **Step 1: Author `docs/kb/codebase-map.md`**

Frontmatter (stamp `last_verified_sha` from `git rev-parse --short HEAD`):

```markdown
---
title: Codebase Map
last_updated: 2026-07-09
last_verified_sha: <short HEAD sha>
sources:
  - src/agentflow/
  - manifests/
  - skills/
  - tests/
status: current
---
```

Required content (use the inventory in this plan's Reference section — every entry must map to a real path):
- `# Codebase Map`
- `## Package layout` — a table or list of each `src/agentflow/` subpackage (`core`, `agents`, `orchestrator`, `llm`, `tools`, `api`, `cli`) → one-line responsibility + key files. Anchor each to its directory with a relative link (`[core](../../src/agentflow/core/)`).
- `## Top-level modules` — `main.py`, `config.py`, `logging_config.py`, `__main__.py` — one line each.
- `## Manifests` — what `manifests/*.yaml` are and the list of agent manifests present.
- `## Skills` — what `skills/` holds (the AgentFlow agent skills) and the list of skill dirs.
- `## Tests` — `tests/` layout and how to run (`uv run pytest`).
- `## Related` — links to [architecture](architecture.md), [concepts](concepts.md).

- [ ] **Step 2: Verify**

Run the reusable verification snippet with `DOC=docs/kb/codebase-map.md`.
Expected: `frontmatter OK`, `sources OK`, empty drift check.

- [ ] **Step 3: Verify every code anchor resolves**

Run:
```bash
grep -oE '\]\(\.\./\.\./[^)]+\)' docs/kb/codebase-map.md | sed -E 's/\]\((.*)\)/\1/' | while read p; do
  test -e "docs/kb/$p" || echo "BROKEN ANCHOR: $p"
done; echo "anchor check done"
```
Expected: `anchor check done` with no `BROKEN ANCHOR` lines.

- [ ] **Step 4: Commit**

```bash
git add docs/kb/codebase-map.md
git commit -m "docs(kb): add codebase map"
```

---

### Task 4: Author `concepts.md`

**Files:**
- Create: `docs/kb/concepts.md`

**Interfaces:**
- Consumes: frontmatter convention from Task 1.
- Produces: the domain glossary linked as `[concepts](concepts.md)`.

- [ ] **Step 1: Read the sources before writing**

Read `src/agentflow/core/models.py`, `src/agentflow/core/registry.py`, `src/agentflow/core/skill_loader.py`, `src/agentflow/tools/registry.py`, `src/agentflow/tools/skills.py`, and one manifest (`manifests/research_agent.yaml`).

- [ ] **Step 2: Author `docs/kb/concepts.md`**

Frontmatter (stamp `last_verified_sha` from `git rev-parse --short HEAD`):

```markdown
---
title: Concepts & Glossary
last_updated: 2026-07-09
last_verified_sha: <short HEAD sha>
sources:
  - src/agentflow/core/models.py
  - src/agentflow/core/registry.py
  - src/agentflow/core/skill_loader.py
  - src/agentflow/tools/
  - manifests/
status: current
---
```

Required content:
- `# Concepts & Glossary`
- One `## <Term>` subsection per core domain concept, each 2-4 lines, grounded in the code you read: **Agent**, **Agent manifest**, **Orchestration** (decompose/plan/schedule), **Task/Subtask** (whatever `core/models.py` actually defines), **Registry** (agent + tool registries), **Skill**, **Tool**, **Message bus**, **Context**. For each, name the defining module.
- `## Related` — links to [architecture](architecture.md), [codebase-map](codebase-map.md).

Only include terms that exist in the code you read. If `models.py` uses different names, use those exact names.

- [ ] **Step 3: Verify**

Run the reusable verification snippet with `DOC=docs/kb/concepts.md`.
Expected: `frontmatter OK`, `sources OK`, empty drift check.

- [ ] **Step 4: Commit**

```bash
git add docs/kb/concepts.md
git commit -m "docs(kb): add concepts and glossary"
```

---

### Task 5: Author `conventions.md`

**Files:**
- Create: `docs/kb/conventions.md`

**Interfaces:**
- Consumes: frontmatter convention from Task 1.
- Produces: the patterns/testing reference linked as `[conventions](conventions.md)`.

- [ ] **Step 1: Observe the actual conventions before writing**

Read `pyproject.toml` (deps, pytest config, tooling), `skills/python-coding/SKILL.md` + `best_practices.md` (the project's own stated Python conventions), two representative test files (`tests/test_agent.py`, `tests/test_scheduler.py`), and `src/agentflow/config.py` + `logging_config.py`.

- [ ] **Step 2: Author `docs/kb/conventions.md`**

Frontmatter (stamp `last_verified_sha` from `git rev-parse --short HEAD`):

```markdown
---
title: Conventions & Patterns
last_updated: 2026-07-09
last_verified_sha: <short HEAD sha>
sources:
  - pyproject.toml
  - src/agentflow/config.py
  - src/agentflow/logging_config.py
  - tests/
  - skills/python-coding/
status: current
---
```

Required content (state only what the code/config actually shows — no invented rules):
- `# Conventions & Patterns`
- `## Tooling` — `uv` for env/deps, Python version (`.python-version`), how to run the app and tests (`uv run pytest`, `uv run ...`).
- `## Code style` — type hints, error handling, logging (`logging_config.py`), config pattern (`config.py`), summarizing `skills/python-coding`.
- `## Async` — note `pytest-asyncio` usage and any async patterns seen in the orchestrator/agents.
- `## Testing` — where tests live, naming (`test_*.py`), how they're structured, async test pattern.
- `## Related` — links to [how-to](how-to.md), [codebase-map](codebase-map.md).

- [ ] **Step 3: Verify**

Run the reusable verification snippet with `DOC=docs/kb/conventions.md`.
Expected: `frontmatter OK`, `sources OK`, empty drift check.

- [ ] **Step 4: Commit**

```bash
git add docs/kb/conventions.md
git commit -m "docs(kb): add conventions and patterns"
```

---

### Task 6: Author `how-to.md`

**Files:**
- Create: `docs/kb/how-to.md`

**Interfaces:**
- Consumes: frontmatter convention from Task 1; concepts from Task 4.
- Produces: task recipes linked as `[how-to](how-to.md)`.

- [ ] **Step 1: Trace the extension points before writing**

Read `manifests/research_agent.yaml` (manifest shape), `src/agentflow/core/registry.py` (how agents register), `src/agentflow/tools/registry.py` + `builtin.py` (how tools register), `src/agentflow/tools/skills.py` + `core/skill_loader.py` (how skills attach). Confirm the actual steps to add each.

- [ ] **Step 2: Author `docs/kb/how-to.md`**

Frontmatter (stamp `last_verified_sha` from `git rev-parse --short HEAD`):

```markdown
---
title: How-To Recipes
last_updated: 2026-07-09
last_verified_sha: <short HEAD sha>
sources:
  - manifests/
  - src/agentflow/core/registry.py
  - src/agentflow/tools/registry.py
  - src/agentflow/tools/skills.py
  - src/agentflow/core/skill_loader.py
status: current
---
```

Required content — each recipe is numbered concrete steps with real file paths derived from what you read:
- `# How-To Recipes`
- `## Add a new agent` — create manifest in `manifests/`, required fields, registration path.
- `## Add a new tool` — where to define, how to register in `tools/registry.py`/`builtin.py`.
- `## Attach a skill to an agent` — how skills are loaded (`skill_loader.py`) and referenced.
- `## Run the system` — the actual run command(s) (from `main.py`/`__main__.py`/`cli/`).
- `## Run tests` — `uv run pytest`, targeted test invocation.
- `## Related` — links to [concepts](concepts.md), [conventions](conventions.md).

- [ ] **Step 3: Verify**

Run the reusable verification snippet with `DOC=docs/kb/how-to.md`.
Expected: `frontmatter OK`, `sources OK`, empty drift check.

- [ ] **Step 4: Commit**

```bash
git add docs/kb/how-to.md
git commit -m "docs(kb): add how-to recipes"
```

---

### Task 7: Author the `update-kb` project skill

**Files:**
- Create: `.claude/skills/update-kb/SKILL.md`

**Interfaces:**
- Consumes: the frontmatter contract (`last_updated`, `last_verified_sha`, `sources`, `status`) and drift query defined across Tasks 1-6.
- Produces: the end-of-task checklist referenced by `CLAUDE.md` (Task 8).

- [ ] **Step 1: Create the skill directory**

```bash
mkdir -p .claude/skills/update-kb
```

- [ ] **Step 2: Author `.claude/skills/update-kb/SKILL.md`**

```markdown
---
name: update-kb
description: Use at the end of any implementation or improvement task to reconcile the docs/kb knowledge base with code changes and refresh freshness metadata before declaring the task complete.
---

# Update Knowledge Base

Run this after completing an implementation/improvement task, before declaring it
done. It keeps `docs/kb/` current with the code.

## Checklist (create one todo per item)

1. **List what changed.** Get the paths your task touched:
   `git diff --name-only <task-start-sha>..HEAD` (or the working tree if uncommitted).

2. **Find affected docs.** For each KB doc, read its frontmatter `sources`. A doc
   is affected if any changed path is at or under one of its `sources` entries.

3. **Reconcile prose.** For each affected doc, update the body so it matches the
   new code reality — names, flow, recipes. Do not duplicate the human docs in
   `docs/` (design.md, gap analysis, presentation); link instead.

4. **Refresh metadata.** On every doc you reconciled, set `last_updated` to today
   and set `last_verified_sha` to current HEAD (`git rev-parse --short HEAD`).

5. **New subsystem?** If the change introduced a whole subsystem worth its own
   doc, create `docs/kb/subsystems/<name>.md` (same frontmatter contract) and link
   it from `docs/kb/index.md`.

6. **Drift sweep.** For EVERY doc in `docs/kb/`, run the drift query:
   `git log --oneline <last_verified_sha>..HEAD -- <that doc's sources>`
   Non-empty output = the doc is behind. Fix it now, or if it genuinely cannot be
   fixed in this task, set its frontmatter `status: stale` so the next run catches it.

7. **Validate.** For each doc you changed, confirm frontmatter still has all five
   keys in order (`title`, `last_updated`, `last_verified_sha`, `sources`,
   `status`), every `sources` path exists, and internal links resolve.

8. **Commit** the KB updates alongside (or right after) your task's changes.

## Drift query reference

```bash
SHA=<doc's last_verified_sha>
git log --oneline ${SHA}..HEAD -- <space-separated sources from that doc>
```
Empty output means the doc is current for its declared sources.
```

- [ ] **Step 3: Verify the skill frontmatter parses**

Run:
```bash
uv run python -c "
import yaml
fm = yaml.safe_load(open('.claude/skills/update-kb/SKILL.md').read().split('---',2)[1])
assert fm['name']=='update-kb', fm
assert 'description' in fm and len(fm['description'])>20
print('skill frontmatter OK:', fm['name'])
"
```
Expected: `skill frontmatter OK: update-kb`.

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/update-kb/SKILL.md
git commit -m "feat(skill): add update-kb knowledge-base maintenance skill"
```

---

### Task 8: Author root `CLAUDE.md` and run full KB validation

**Files:**
- Create: `CLAUDE.md`

**Interfaces:**
- Consumes: `docs/kb/index.md` (Task 1) and the `update-kb` skill (Task 7).
- Produces: the discovery entry point + standing rules; final validation of the whole KB.

- [ ] **Step 1: Author `CLAUDE.md`**

```markdown
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
```

- [ ] **Step 2: Validate the entire KB in one pass**

Run:
```bash
uv run python -c "
import os, yaml, glob, subprocess
docs = glob.glob('docs/kb/**/*.md', recursive=True)
assert docs, 'no KB docs found'
head = subprocess.check_output(['git','rev-parse','--short','HEAD']).decode().strip()
problems = []
for d in docs:
    raw = open(d).read()
    if not raw.startswith('---'):
        problems.append(f'{d}: no frontmatter'); continue
    fm = yaml.safe_load(raw.split('---',2)[1])
    for k in ['title','last_updated','last_verified_sha','sources','status']:
        if k not in fm: problems.append(f'{d}: missing {k}')
    for p in fm.get('sources',[]):
        if not os.path.exists(p): problems.append(f'{d}: missing source {p}')
    # internal .md links resolve
    import re
    for m in re.findall(r']\(([^)]+\.md)\)', raw):
        if m.startswith('http'): continue
        target = os.path.normpath(os.path.join(os.path.dirname(d), m))
        if not os.path.exists(target): problems.append(f'{d}: broken link -> {m}')
print('checked', len(docs), 'docs')
print('PROBLEMS:' , problems if problems else 'none')
assert not problems, problems
"
```
Expected: `checked 6 docs`, `PROBLEMS: none`.

- [ ] **Step 3: Confirm the drift query returns empty for all docs**

Run:
```bash
uv run python -c "
import yaml, glob, subprocess
for d in glob.glob('docs/kb/**/*.md', recursive=True):
    fm = yaml.safe_load(open(d).read().split('---',2)[1])
    out = subprocess.check_output(['git','log','--oneline', fm['last_verified_sha']+'..HEAD','--']+fm['sources']).decode().strip()
    print(d, '->', 'CURRENT' if not out else 'STALE:\n'+out)
"
```
Expected: every doc prints `-> CURRENT`.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add CLAUDE.md with knowledge-base read + update rules"
```

---

## Self-Review

**Spec coverage:**
- Location & structure (`docs/kb/` + core docs + `subsystems/`) → Tasks 1-6. ✓
- Fully separate from existing docs → Global Constraints + index/CLAUDE.md wording. ✓
- Document format (frontmatter fields, interlinks, code anchors) → Global Constraints + every doc task. ✓
- Drift detection (`git log <sha>..HEAD -- sources`) → verification snippet, Task 7 skill, Task 8 Step 3. ✓
- Enforcement: CLAUDE.md rules → Task 8; self-review skill → Task 7. ✓
- Layered core + `subsystems/` growth valve → Task 1 (`.gitkeep`), skill Step 5. ✓
- Freshness metadata + source anchors → all doc tasks; `status: stale` fallback → skill Step 6. ✓
- Success criteria (orient without source, seeded frontmatter, CLAUDE.md+skill consistent, drift query works) → Task 8 validation. ✓

**Placeholder scan:** `<short HEAD sha>` and `<task-start-sha>` are intentional runtime values with explicit instructions on how to obtain them, not unfilled placeholders. Doc prose is specified by required-section + read-these-sources rather than verbatim text because the content must be derived from live code; this is appropriate for a seed-from-codebase doc task. No `TBD`/`TODO`/"handle edge cases".

**Type consistency:** Frontmatter key set and order is identical across all tasks and the validator. Doc filenames referenced in `index.md` (Task 1 Step 4) match the files created in Tasks 2-6. The `update-kb` skill name matches its directory and the `CLAUDE.md` reference.
