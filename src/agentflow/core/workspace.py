"""Per-run workspace resolution and isolation.

Every run executes its tools inside one directory under ``settings.workspace_dir``:

- a **named** workspace (``workspace/<name>/``) when the request names one — a
  long-lived project that successive runs iterate on;
- otherwise a **fresh per-run** workspace (``workspace/runs/<run_id>/``), so
  concurrent unrelated runs can never overwrite each other's files.

The engine resolves the directory once per run and arms ``_current_workspace``;
the built-in tools read it through ``current_workspace()``. Code running outside
a run (unit tests, ad-hoc tool calls) falls back to the workspace root.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

from agentflow.config import settings

# Fresh per-run workspaces live under this subdirectory of the workspace root,
# so the name is reserved and cannot be used for a named workspace.
RUNS_SUBDIR = "runs"

# One path segment: no separators, no leading dot (so no "..", no hidden dirs).
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

# Relative path that addresses the workspace root itself. Only used for runs
# recorded before per-run workspaces existed (meta.json without a workspace).
LEGACY_ROOT = "."


class InvalidWorkspaceError(ValueError):
    """Raised when a workspace name or recorded path is unsafe or reserved."""


def validate_workspace_name(name: str) -> str:
    """Return *name* if it is a valid named-workspace identifier, else raise."""
    if not _NAME_RE.fullmatch(name):
        raise InvalidWorkspaceError(
            f"Invalid workspace name {name!r}: use 1-64 letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    if name == RUNS_SUBDIR:
        raise InvalidWorkspaceError(f"Workspace name {RUNS_SUBDIR!r} is reserved")
    return name


def run_workspace_rel(run_id: str, name: str | None) -> str:
    """Return the workspace path (relative to the root) a run should use."""
    if name is not None:
        return validate_workspace_name(name)
    return f"{RUNS_SUBDIR}/{run_id}"


def workspace_root() -> Path:
    return Path(settings.workspace_dir).expanduser().resolve()


def runs_root() -> Path:
    """Directory holding every run's records (``<runs_root>/<run_id>/``) and campaigns."""
    return Path(settings.runs_dir).expanduser().resolve()


class DataDirConflictError(ValueError):
    """Raised when the runs directory and the workspace root overlap."""


def check_data_dirs() -> None:
    """Refuse a layout where agents could reach run records, or records hold workspaces.

    Runs recorded before per-run workspaces work in the workspace root itself
    (``LEGACY_ROOT``), so a runs dir inside it would expose session logs and campaign
    ledgers to agent tools. Called at API and worker startup.
    """
    ws, runs = workspace_root(), runs_root()
    if ws.is_relative_to(runs) or runs.is_relative_to(ws):
        raise DataDirConflictError(
            f"RUNS_DIR ({runs}) and WORKSPACE_DIR ({ws}) must not contain each other; "
            "use sibling directories such as <AGENTFLOW_HOME>/runs and <AGENTFLOW_HOME>/workspace"
        )


def workspace_path(rel: str) -> Path:
    """Resolve a recorded workspace-relative path, refusing anything outside the root."""
    root = workspace_root()
    target = (root / rel).resolve()
    if not target.is_relative_to(root):
        raise InvalidWorkspaceError(f"Workspace path {rel!r} escapes the workspace root")
    return target


# Set by the engine for the lifetime of a run; inherited by every asyncio task
# the run spawns (planner, subtasks, decomposer, KB dispatch).
_current_workspace: ContextVar[Path | None] = ContextVar("_current_workspace", default=None)


def current_workspace() -> Path:
    """Return the active run's workspace (created on demand), or the root outside a run."""
    ws = _current_workspace.get() or workspace_root()
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def run_relative(path: str) -> str | None:
    """Normalise a tool-supplied *path* to POSIX form relative to the active run's
    workspace, or None if it points outside it.

    Agents may name the same file as ``report.md``, ``./a/../report.md`` or an
    absolute path inside the workspace; artifact records and ``files_written``
    store one canonical form so they never depend on where the root lives.
    """
    ws = current_workspace().resolve()
    target = (ws / path).resolve()
    if not target.is_relative_to(ws) or target == ws:
        return None
    return target.relative_to(ws).as_posix()


class WorkspaceLocks:
    """Serialises runs that share a workspace (named workspaces, or followups).

    Process-local: under ``STATE_BACKEND=redis`` two replicas can still run in the
    same named workspace concurrently. Entries are dropped once no run holds or
    waits on them, so per-run keys do not accumulate.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._users: dict[str, int] = {}

    def is_busy(self, key: str) -> bool:
        lock = self._locks.get(key)
        return lock is not None and lock.locked()

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._users[key] = self._users.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._users[key] -= 1
            if self._users[key] == 0:
                del self._users[key]
                del self._locks[key]
