"""Git checkpoints of a run's workspace.

Each run snapshots its workspace into a **shadow repository** at
``<runs_dir>/<run_id>/checkpoints.git`` (work tree = the run's workspace): a
baseline when the run starts or resumes, then one commit per finished subtask.
The shadow repo never touches a ``.git`` the workspace may contain — a named
workspace is often the user's own project — while that project's ``.gitignore``
files still apply.

Checkpoints give the engine a clean rollback for failed attempts
(``restore_paths``) and give people an audit trail of what each subtask changed;
any snapshot can be restored by hand with
``git --git-dir <runs_dir>/<run_id>/checkpoints.git --work-tree <workspace> checkout <sha> -- .``.

Git failures never fail a run: ``open()`` returns None (checkpoints off) and a
failed commit or restore is logged.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from agentflow.config import settings

logger = logging.getLogger(__name__)

CHECKPOINTS_DIR = "checkpoints.git"

# Heavy, regenerable trees no checkpoint should copy (on top of the project's own
# .gitignore rules). Never a deliverable.
_DEFAULT_EXCLUDES = (
    "node_modules/", ".venv/", "venv/", "__pycache__/", "*.pyc", ".mypy_cache/",
    ".pytest_cache/", ".ruff_cache/", ".tool_output/", ".DS_Store",
)


class CheckpointError(Exception):
    """A git command failed."""


@dataclass(frozen=True)
class Checkpoint:
    sha: str
    label: str
    changed: list[str] = field(default_factory=list)


class WorkspaceCheckpoints:
    """Serialised git operations on one run's shadow checkpoint repository."""

    def __init__(self, git_dir: Path, work_tree: Path) -> None:
        self._git_dir = git_dir
        self._work_tree = work_tree
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, run_id: str, work_tree: Path) -> WorkspaceCheckpoints | None:
        """Open (initialising on first use) the run's checkpoint repo; None if unavailable."""
        if shutil.which("git") is None:
            logger.warning("[%s] git not found — workspace checkpoints disabled", run_id)
            return None
        git_dir = Path(settings.runs_dir).resolve() / run_id / CHECKPOINTS_DIR
        cp = cls(git_dir, work_tree)
        try:
            if not (git_dir / "HEAD").exists():
                work_tree.mkdir(parents=True, exist_ok=True)
                git_dir.parent.mkdir(parents=True, exist_ok=True)
                await cp._git("init", "--quiet")
                (git_dir / "info").mkdir(parents=True, exist_ok=True)
                (git_dir / "info" / "exclude").write_text("\n".join(_DEFAULT_EXCLUDES) + "\n")
        except (CheckpointError, OSError) as exc:
            logger.warning("[%s] Could not initialise workspace checkpoints: %s", run_id, exc)
            return None
        return cp

    async def _git(self, *args: str) -> str:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "GIT_DIR": str(self._git_dir),
            "GIT_WORK_TREE": str(self._work_tree),
            # Isolate from the user's git config (signing, hooks, templates, aliases).
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        proc = await asyncio.create_subprocess_exec(
            "git", "-c", "user.name=AgentFlow", "-c", "user.email=agentflow@localhost",
            "-c", "commit.gpgsign=false", "-c", f"core.hooksPath={os.devnull}",
            "-c", "core.autocrlf=false", "-c", "init.defaultBranch=checkpoints",
            *args,
            cwd=str(self._work_tree),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise CheckpointError(f"git {args[0]} failed: {err.decode(errors='replace').strip()}")
        return out.decode(errors="replace")

    async def head(self) -> str | None:
        try:
            return (await self._git("rev-parse", "--verify", "--quiet", "HEAD")).strip() or None
        except CheckpointError:
            return None  # no commits yet

    async def commit(self, label: str) -> Checkpoint | None:
        """Snapshot the whole workspace. None when nothing changed since the last one."""
        async with self._lock:
            await self._git("add", "--all")
            if not (await self._git("status", "--porcelain")).strip():
                return None
            await self._git("commit", "--quiet", "--no-verify", "-m", label)
            sha = (await self._git("rev-parse", "HEAD")).strip()
            changed = (await self._git("diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha)).split()
            return Checkpoint(sha=sha, label=label, changed=changed)

    async def restore_paths(self, sha: str | None, paths: list[str]) -> list[str]:
        """Put *paths* back as they were at *sha* (deleting ones that did not exist).

        ``sha=None`` means there was no snapshot yet (an empty workspace), so every
        path is new and is removed.

        Only the given paths are touched — other subtasks may be writing elsewhere in
        the same workspace concurrently. Returns the paths actually restored/removed.
        """
        restored: list[str] = []
        root = self._work_tree.resolve()
        async with self._lock:
            for rel in dict.fromkeys(paths):
                target = (root / rel).resolve()
                if not target.is_relative_to(root) or target == root:
                    logger.warning("Refusing to restore path outside the workspace: %r", rel)
                    continue
                rel_posix = target.relative_to(root).as_posix()
                existed = sha is not None and bool(
                    (await self._git("ls-tree", "--name-only", sha, "--", rel_posix)).strip()
                )
                if existed:
                    await self._git("checkout", sha, "--", rel_posix)
                elif target.is_file():
                    target.unlink()
                else:
                    continue
                restored.append(rel_posix)
        return restored
