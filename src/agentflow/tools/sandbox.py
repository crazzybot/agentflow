"""Where agent-issued code runs — the "hands", kept apart from the harness.

Execution tools (``bash_exec``, ``bash_exec_readonly``, ``python_exec``) call
``current_sandbox().run(argv, timeout_s=...)`` instead of spawning processes
themselves. Tools that hold credentials (web search, MCP servers) are not routed
here: they stay in the harness process, so a sandbox never sees a secret.

Backends (``SANDBOX_BACKEND``):

- ``local`` (default): a subprocess on this host, in the run's workspace, with a
  scrubbed environment; on timeout the whole process group is killed.
- ``docker``: one container per run, started lazily on the first exec call, with
  the run's workspace bind-mounted at ``/workspace`` (so file tools, checkpoints
  and the verifier see the same files), no capabilities, no new privileges,
  memory/CPU/PID limits, the host user's uid:gid, and no host environment beyond
  ``SANDBOX_ENV_PASSTHROUGH``. If Docker is unavailable, exec tools fail rather
  than silently running on the host.

The engine arms ``_current_sandbox`` with a per-run ``SandboxProvider`` and closes
it when the run ends; outside a run a ``LocalSandbox`` on the current workspace
is used.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agentflow.config import settings
from agentflow.core.workspace import current_workspace

logger = logging.getLogger(__name__)

CONTAINER_WORKSPACE = "/workspace"

# Environment variables passed through to local agent subprocesses. Everything
# else — notably ANTHROPIC_API_KEY, TAVILY_API_KEY and REDIS_URL — is withheld, so
# a prompt-injected command cannot read credentials and exfiltrate them. Extend
# via SANDBOX_ENV_PASSTHROUGH when a toolchain needs more.
_LOCAL_ENV_ALLOWLIST = frozenset({
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "TMPDIR", "USER", "LOGNAME", "SHELL",
})


class SandboxUnavailableError(Exception):
    """The configured sandbox backend cannot run commands."""


@dataclass(frozen=True)
class ExecResult:
    exit_code: int | None  # None when the command was killed for timing out
    output: str            # stdout and stderr, interleaved
    timed_out: bool = False


class Sandbox(Protocol):
    async def run(self, argv: list[str], *, timeout_s: float) -> ExecResult: ...

    async def close(self) -> None: ...


def local_env(workspace: Path) -> dict[str, str]:
    allowed = _LOCAL_ENV_ALLOWLIST | set(settings.sandbox_env_passthrough)
    env = {k: v for k, v in os.environ.items() if k in allowed}
    # Keep any residual ~ expansion (and tool caches such as npm/uv) inside the workspace
    env["HOME"] = str(workspace)
    return env


class LocalSandbox:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    async def run(self, argv: list[str], *, timeout_s: float) -> ExecResult:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(self.workspace),
            env=local_env(self.workspace),
            start_new_session=True,  # own process group, so a timeout kills children too
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            _kill_group(proc.pid)
            await proc.wait()
            return ExecResult(exit_code=None, output="", timed_out=True)
        return ExecResult(exit_code=proc.returncode, output=out.decode(errors="replace"))

    async def close(self) -> None:
        return None


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class DockerSandbox:
    """One long-lived container per run; commands run via ``docker exec``."""

    def __init__(self, workspace: Path, run_id: str) -> None:
        self.workspace = workspace
        self.name = f"agentflow-{run_id[:12]}-{uuid.uuid4().hex[:6]}"
        self._started = False
        self._lock = asyncio.Lock()

    def _run_args(self) -> list[str]:
        args = [
            "docker", "run", "--detach", "--rm", "--name", self.name,
            "--network", settings.sandbox_docker_network,
            "--memory", settings.sandbox_docker_memory,
            "--cpus", str(settings.sandbox_docker_cpus),
            "--pids-limit", str(settings.sandbox_docker_pids_limit),
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--volume", f"{self.workspace.resolve()}:{CONTAINER_WORKSPACE}",
            "--workdir", CONTAINER_WORKSPACE,
            "--env", f"HOME={CONTAINER_WORKSPACE}",
        ]
        if hasattr(os, "getuid"):
            args += ["--user", f"{os.getuid()}:{os.getgid()}"]
        for name in settings.sandbox_env_passthrough:
            if name in os.environ:
                args += ["--env", name]  # value copied from this process, by explicit opt-in
        return [*args, settings.sandbox_docker_image, "sleep", "infinity"]

    async def _ensure_started(self) -> None:
        async with self._lock:
            if self._started:
                return
            if shutil.which("docker") is None:
                raise SandboxUnavailableError("SANDBOX_BACKEND=docker but the docker CLI is not installed")
            self.workspace.mkdir(parents=True, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                *self._run_args(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                raise SandboxUnavailableError(f"could not start sandbox container: {err.decode(errors='replace').strip()}")
            self._started = True
            logger.info("Started sandbox container %s for %s", self.name, self.workspace)

    async def run(self, argv: list[str], *, timeout_s: float) -> ExecResult:
        await self._ensure_started()
        seconds = max(1, int(timeout_s))
        # `timeout` runs inside the container: killing the docker CLI on the host
        # would leave the command running in there.
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "--interactive", self.name, "timeout", "-k", "5", str(seconds), *argv,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=seconds + 15)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ExecResult(exit_code=None, output="", timed_out=True)
        # 124/137: GNU timeout's codes for "timed out" / "killed after the grace period"
        if proc.returncode in (124, 137):
            return ExecResult(exit_code=None, output=out.decode(errors="replace"), timed_out=True)
        return ExecResult(exit_code=proc.returncode, output=out.decode(errors="replace"))

    async def close(self) -> None:
        if not self._started:
            return
        proc = await asyncio.create_subprocess_exec(
            "docker", "rm", "--force", self.name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("Could not remove sandbox container %s: %s", self.name, err.decode(errors="replace").strip())
        self._started = False


def python_interpreter() -> str:
    """Interpreter for python_exec in the configured backend."""
    if settings.sandbox_backend == "docker":
        return settings.sandbox_docker_python
    configured = settings.sandbox_python.strip()
    if not configured:
        return "python3"
    p = Path(configured)
    if not p.is_absolute():
        p = Path.cwd() / p
    if p.exists():
        return str(p)
    logger.warning("sandbox_python %r not found, falling back to python3", configured)
    return "python3"


class SandboxProvider:
    """Per-run sandbox, created on first use and closed when the run ends."""

    def __init__(self, workspace: Path, run_id: str) -> None:
        self.workspace = workspace
        self.run_id = run_id
        self._sandbox: Sandbox | None = None

    def get(self) -> Sandbox:
        if self._sandbox is None:
            self._sandbox = make_sandbox(self.workspace, self.run_id)
        return self._sandbox

    async def close(self) -> None:
        if self._sandbox is not None:
            await self._sandbox.close()


def make_sandbox(workspace: Path, run_id: str) -> Sandbox:
    backend = settings.sandbox_backend
    if backend == "docker":
        return DockerSandbox(workspace, run_id)
    if backend != "local":
        raise SandboxUnavailableError(f"Unknown SANDBOX_BACKEND {backend!r} (expected 'local' or 'docker')")
    return LocalSandbox(workspace)


_current_sandbox: ContextVar[SandboxProvider | None] = ContextVar("_current_sandbox", default=None)


class _EphemeralSandbox:
    """For calls outside a run (no provider to close it later): one sandbox per command."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    async def run(self, argv: list[str], *, timeout_s: float) -> ExecResult:
        sandbox = make_sandbox(self.workspace, "adhoc")
        try:
            return await sandbox.run(argv, timeout_s=timeout_s)
        finally:
            await sandbox.close()

    async def close(self) -> None:
        return None


def current_sandbox() -> Sandbox:
    provider = _current_sandbox.get()
    if provider is None:
        return _EphemeralSandbox(current_workspace())
    return provider.get()
