---
name: python-coding
description: Best practices for writing production-quality Python and managing projects with uv.
---

# Python Coding

Best practices for writing production-quality Python and managing projects with uv.

## Reference Documents

- `best_practices.md` — Type hints, error handling, logging, testing, and code style conventions
- `uv_guide.md` — Project creation, dependency management, virtual environments, and scripting with uv

---

## Overview

Use this skill when writing, reviewing, or structuring Python code. It covers the full
lifecycle from project creation through to tested, deployable code.

### When to use `best_practices.md`

- Writing new modules, classes, or functions
- Reviewing existing code for quality issues
- Setting up logging, error handling, or test structure
- Deciding between patterns (dataclass vs. Pydantic, sync vs. async, etc.)

### When to use `uv_guide.md`

- Creating a new Python project
- Adding, removing, or pinning dependencies
- Running scripts or tools without activating a venv
- Setting up a workspace with multiple packages
- Generating or updating a lockfile for reproducible installs

### General Principles

- **Explicit over implicit** — Use type hints everywhere; avoid `Any` unless unavoidable.
- **Fail loudly** — Raise specific exceptions with useful messages; never silently swallow errors.
- **Small, focused units** — Functions do one thing; modules have a single responsibility.
- **Test at the boundary** — Unit-test pure functions; integration-test I/O boundaries.
- **Reproducible environments** — Always commit `uv.lock`; never pin versions only in prose.
