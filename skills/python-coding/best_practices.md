# Python Best Practices

Production-quality conventions for Python 3.11+.

## Type Hints

Always annotate public functions and class attributes. Use `from __future__ import annotations`
at the top of every file so forward references work without quotes.

```python
from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def process(items: Sequence[str], limit: int = 100) -> list[str]:
    return [item.strip() for item in items[:limit]]
```

- Prefer `collections.abc` types (`Sequence`, `Mapping`, `Callable`) over `typing` equivalents.
- Use `X | Y` union syntax (Python 3.10+) instead of `Union[X, Y]`.
- Use `X | None` instead of `Optional[X]`.
- Avoid bare `Any`; use `object` for truly unknown types or create a `TypeAlias`.

## Data Classes and Pydantic

| Use case | Preferred type |
|----------|---------------|
| Simple immutable data container | `@dataclass(frozen=True)` |
| Mutable internal struct | `@dataclass` |
| API / config / validated input | `pydantic.BaseModel` |
| Enum-like choices | `enum.Enum` or `enum.StrEnum` |

```python
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass
class Config:
    host: str = "localhost"
    port: int = 8080
    tags: list[str] = field(default_factory=list)
```

## Error Handling

- Raise specific, narrow exception types; catch the same.
- Never catch `Exception` at a call site unless you re-raise or log with full traceback.
- Add context to exceptions with `raise SomeError("detail") from original_exc`.

```python
class ConfigError(ValueError):
    """Raised when a configuration value is invalid."""


def load_config(path: str) -> dict[str, Any]:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc
```

## Logging

Use the standard `logging` module. Never use `print()` in library or application code.

```python
import logging

logger = logging.getLogger(__name__)  # always use __name__


def do_work(item: str) -> None:
    logger.debug("Processing item: %s", item)   # lazy formatting
    try:
        result = _process(item)
        logger.info("Processed %s → %s", item, result)
    except Exception:
        logger.exception("Failed to process %r", item)  # includes traceback
        raise
```

- Configure logging once at the application entry point with `logging.basicConfig(...)`.
- Use `logger.exception(...)` (not `logger.error(...)`) inside `except` blocks to capture tracebacks.
- Use `%s` formatting args, not f-strings, so formatting is skipped when the level is disabled.

## Project Structure

```
my_project/
├── pyproject.toml          # single source of truth for metadata + dependencies
├── uv.lock                 # committed; ensures reproducible installs
├── README.md
├── src/
│   └── my_package/
│       ├── __init__.py
│       ├── core.py
│       └── cli.py
└── tests/
    ├── conftest.py
    └── test_core.py
```

- Put source under `src/` (src layout) to avoid accidental imports of the uninstalled package.
- One top-level package per project; avoid `__init__.py` files that do heavy work.

## Testing

Use `pytest`. Name test files `test_*.py`; name test functions `test_<what>_<condition>`.

```python
# tests/test_core.py
import pytest
from my_package.core import process


def test_process_strips_whitespace():
    assert process(["  hello  "]) == ["hello"]


def test_process_respects_limit():
    items = [str(i) for i in range(200)]
    assert len(process(items, limit=50)) == 50


def test_process_empty_input():
    assert process([]) == []


@pytest.mark.parametrize("bad_input", [None, 42, "string"])
def test_process_rejects_non_sequence(bad_input):
    with pytest.raises(TypeError):
        process(bad_input)
```

- Use `pytest.fixture` for shared setup; avoid `setUp`/`tearDown`.
- Use `tmp_path` (built-in fixture) for file system tests.
- Keep tests independent — no shared mutable state between tests.

## Async Code

```python
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


async def fetch_all(urls: list[str]) -> list[str]:
    async with asyncio.TaskGroup() as tg:       # Python 3.11+
        tasks = [tg.create_task(fetch(u)) for u in urls]
    return [t.result() for t in tasks]


@asynccontextmanager
async def managed_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client
```

- Use `asyncio.TaskGroup` (3.11+) instead of `asyncio.gather` for structured concurrency.
- Avoid mixing sync and async — if a function does I/O it should be async.
- Use `asynccontextmanager` for async resource management.

## Common Anti-Patterns to Avoid

| Anti-pattern | Fix |
|-------------|-----|
| `except Exception: pass` | Log and re-raise, or raise a domain error |
| Mutable default argument `def f(x=[])` | Use `def f(x=None): x = x or []` or `field(default_factory=list)` |
| `import *` | Explicit imports only |
| Hard-coded credentials or paths | Use env vars via `pydantic_settings` or `os.environ` |
| `time.sleep` in async code | Use `await asyncio.sleep` |
| `os.path` in new code | Use `pathlib.Path` |
| `str` for structured data | Use dataclasses or Pydantic models |
