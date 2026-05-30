# uv Project Management Guide

`uv` is a fast Python package and project manager written in Rust. It replaces pip, pip-tools,
pipenv, pyenv, and virtualenv for most workflows.

## Installation

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# or on macOS
brew install uv
```

## Creating a New Project

```bash
# Application (has __main__.py, builds to an executable)
uv init my_app --app

# Library (has src/ layout, publishable to PyPI)
uv init my_lib --lib

# Minimal (no src layout, just a script)
uv init my_script
```

`uv init` creates:
- `pyproject.toml` — project metadata and dependencies
- `.python-version` — pins the Python version
- `README.md`
- `src/my_lib/` (for `--lib`) or `hello.py` (for minimal)

## pyproject.toml Structure

```toml
[project]
name = "my-app"
version = "0.1.0"
description = "Short description"
requires-python = ">=3.11"
dependencies = [
    "httpx>=0.27",
    "pydantic>=2.7",
]

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.23",
    "ruff>=0.4",
    "mypy>=1.10",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.uv]
dev-dependencies = [   # uv-native dev deps (preferred over optional-dependencies for dev)
    "pytest>=8",
    "ruff>=0.4",
]
```

## Dependency Management

```bash
# Add a runtime dependency
uv add httpx
uv add "pydantic>=2.7,<3"

# Add a dev-only dependency
uv add --dev pytest ruff mypy

# Add with extras
uv add "fastapi[standard]"

# Remove a dependency
uv remove httpx

# Upgrade a specific package
uv add httpx --upgrade

# Upgrade all packages within constraints
uv lock --upgrade

# Sync the environment to match uv.lock exactly
uv sync

# Sync including dev dependencies
uv sync --dev
```

## Virtual Environments

uv creates and manages `.venv` automatically. You rarely need to interact with it directly.

```bash
# Create/recreate the venv (runs automatically on first uv sync/run)
uv venv

# Create with a specific Python version
uv venv --python 3.12

# List available Python versions
uv python list

# Pin the project to a specific Python version
uv python pin 3.12
```

## Running Code

```bash
# Run a script (auto-installs deps, no manual activation needed)
uv run python src/my_app/main.py

# Run a module
uv run python -m my_app

# Run a tool (installed ad-hoc, not added to project)
uv run pytest
uv run ruff check .
uv run mypy src/

# Run with extra deps without adding them to pyproject.toml
uv run --with rich python -c "import rich; rich.print('[green]ok[/]')"
```

## Scripts with Inline Dependencies

uv supports PEP 723 inline script metadata — ideal for one-off scripts:

```python
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx",
#   "rich",
# ]
# ///

import httpx
from rich import print

resp = httpx.get("https://httpbin.org/json")
print(resp.json())
```

```bash
uv run my_script.py   # uv reads the inline metadata and installs deps automatically
```

## Lock File

`uv.lock` is the single source of truth for reproducible installs.

```bash
# Generate / update the lockfile (run after changing pyproject.toml)
uv lock

# Install exactly what is in the lockfile (CI / production)
uv sync --frozen

# Check if lockfile is up to date (for CI assertion)
uv lock --check
```

**Always commit `uv.lock`** to version control. It records the exact resolved version of
every transitive dependency, enabling bit-for-bit reproducible environments.

## Workspaces (Monorepo)

A workspace groups multiple packages under one `uv.lock`.

```toml
# Root pyproject.toml
[tool.uv.workspace]
members = ["packages/*"]
```

```bash
uv sync                          # sync all workspace members
uv run --package my_lib pytest   # run pytest for a specific member
```

## Common Workflows

### New project from scratch

```bash
uv init my_project --lib
cd my_project
uv add pydantic httpx
uv add --dev pytest ruff mypy
uv run pytest   # runs tests; creates .venv automatically
```

### Reproduce an existing project

```bash
git clone <repo>
cd <repo>
uv sync --frozen   # installs exactly what uv.lock specifies; no surprises
uv run pytest
```

### One-off tool execution (no installation)

```bash
uv tool run ruff check .
uvx ruff check .        # uvx is shorthand for uv tool run
```

### Checking and formatting in CI

```bash
uv run ruff check --select ALL .
uv run ruff format --check .
uv run mypy src/
uv run pytest --tb=short
```

## Key Differences from pip / poetry

| Task | pip + venv | poetry | uv |
|------|-----------|--------|----|
| Create project | manual | `poetry new` | `uv init` |
| Install deps | `pip install -r requirements.txt` | `poetry install` | `uv sync` |
| Add dependency | edit + `pip install` | `poetry add` | `uv add` |
| Lock file | `pip-compile` (separate tool) | `poetry.lock` | `uv.lock` (built-in) |
| Run in env | activate first | `poetry run` | `uv run` (no activation needed) |
| Speed | slow | moderate | very fast (Rust, parallel) |
