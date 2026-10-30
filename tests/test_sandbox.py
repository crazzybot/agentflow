"""Tests for execution sandboxes (local and docker backends)."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import agentflow.tools  # noqa: F401 — registers built-ins
from agentflow.core.bus import TaskBus
from agentflow.core.context import ContextStore
from agentflow.core.models import AgentResult, AgentStatus, ExecutionPlan, Subtask
from agentflow.core.registry import AgentRegistry
from agentflow.core.workspace import _current_workspace
from agentflow.orchestrator.engine import OrchestratorEngine
from agentflow.orchestrator.stream import StreamRegistry
from agentflow.tools.registry import tool_registry
from agentflow.tools.sandbox import (
    DockerSandbox,
    ExecResult,
    LocalSandbox,
    SandboxProvider,
    SandboxUnavailableError,
    _current_sandbox,
    make_sandbox,
)


@pytest.fixture
def ws(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path / "workspace"))
    monkeypatch.setattr("agentflow.config.settings.runs_dir", str(tmp_path / "runs"))
    d = tmp_path / "workspace"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Local backend
# ---------------------------------------------------------------------------

async def test_bash_exec_runs_in_workspace_without_credentials(ws, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    out = await tool_registry.execute(
        "bash_exec", {"command": 'pwd; echo "key=${ANTHROPIC_API_KEY:-unset}"', "purpose": "t"}
    )
    assert out.startswith("exit_code=0")
    assert str(ws.resolve()) in out and "key=unset" in out


async def test_python_exec_goes_through_the_sandbox(ws, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.sandbox_python", "")
    out = await tool_registry.execute("python_exec", {"code": "print(6 * 7)", "purpose": "t"})
    assert out == "exit_code=0\n42\n"


async def test_timeout_kills_the_command(ws):
    # Previously a timed-out command kept running in the background.
    result = await LocalSandbox(ws).run(["sh", "-c", "sleep 1; touch late.txt"], timeout_s=0.2)
    assert result.timed_out and result.exit_code is None
    await asyncio.sleep(1.3)
    assert not (ws / "late.txt").exists()


async def test_timeout_is_reported_to_the_model(ws):
    out = await tool_registry.execute(
        "bash_exec", {"command": "sleep 5", "purpose": "t", "timeout_seconds": 1}
    )
    assert "timed out after 1s" in out and "killed" in out


def test_unknown_backend_is_rejected(ws, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.sandbox_backend", "vm")
    with pytest.raises(SandboxUnavailableError):
        make_sandbox(ws, "r")


# ---------------------------------------------------------------------------
# Docker backend (with a fake docker CLI)
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, returncode: int = 0, out: bytes = b"") -> None:
        self.returncode, self._out = returncode, out

    async def communicate(self):
        return self._out, b""

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


def _fake_docker(calls: list[list[str]], exec_out: bytes = b"hello\n", exec_rc: int = 0):
    async def create(*argv, **_kw):
        calls.append(list(argv))
        if argv[1] == "exec":
            return _FakeProc(exec_rc, exec_out)
        return _FakeProc(0, b"container-id\n")
    return create


def test_container_is_locked_down(ws, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.sandbox_env_passthrough", ["NODE_OPTIONS"])
    monkeypatch.setenv("NODE_OPTIONS", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    args = DockerSandbox(ws, "run-123")._run_args()
    joined = " ".join(args)
    for flag in ("--cap-drop ALL", "--security-opt no-new-privileges", "--pids-limit", "--memory", "--rm"):
        assert flag in joined
    assert f"{ws.resolve()}:/workspace" in args
    assert "--env NODE_OPTIONS" in joined  # opted in by name
    assert "ANTHROPIC_API_KEY" not in joined
    assert args[-2:] == ["sleep", "infinity"]


async def test_container_starts_lazily_once_and_is_removed(ws, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr("agentflow.tools.sandbox.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr("agentflow.tools.sandbox.asyncio.create_subprocess_exec", _fake_docker(calls))
    sb = DockerSandbox(ws, "run-123")
    await sb.close()
    assert calls == []  # never used → never started

    first = await sb.run(["sh", "-c", "echo hello"], timeout_s=30)
    await sb.run(["ls"], timeout_s=5)
    await sb.close()

    assert first == ExecResult(exit_code=0, output="hello\n")
    assert [c[1] for c in calls] == ["run", "exec", "exec", "rm"]
    assert calls[1][3:8] == [sb.name, "timeout", "-k", "5", "30"]  # timeout enforced in-container


async def test_in_container_timeout_is_reported(ws, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr("agentflow.tools.sandbox.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr("agentflow.tools.sandbox.asyncio.create_subprocess_exec", _fake_docker(calls, exec_rc=124))
    result = await DockerSandbox(ws, "r").run(["sleep", "99"], timeout_s=1)
    assert result.timed_out


async def test_docker_backend_fails_closed(ws, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.sandbox_backend", "docker")
    monkeypatch.setattr("agentflow.tools.sandbox.shutil.which", lambda _: None)
    out = await tool_registry.execute("bash_exec", {"command": "touch ran-on-host.txt", "purpose": "t"})
    assert "sandbox unavailable" in out
    assert not (ws / "ran-on-host.txt").exists()


# ---------------------------------------------------------------------------
# Engine: one lazily-created sandbox per run, closed at the end
# ---------------------------------------------------------------------------

class _RecordingSandbox:
    instances: list["_RecordingSandbox"] = []

    def __init__(self, workspace, run_id) -> None:
        self.workspace, self.run_id, self.calls, self.closed = workspace, run_id, 0, False
        _RecordingSandbox.instances.append(self)

    async def run(self, argv, *, timeout_s):
        self.calls += 1
        return ExecResult(exit_code=0, output="ok")

    async def close(self):
        self.closed = True


class _ExecAgent:
    def __init__(self, execs: int) -> None:
        self.execs = execs

    async def run(self, envelope, emitter, resume_messages=None, ctx=None):
        for _ in range(self.execs):
            await tool_registry.execute("bash_exec", {"command": "true", "purpose": "t"})
        return AgentResult(task_id=envelope.task_id, agent_id="A", status=AgentStatus.success)


@pytest.fixture
def engine(ws, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.capture_events", False)
    monkeypatch.setattr("agentflow.config.settings.capture_results", False)
    monkeypatch.setattr("agentflow.config.settings.direct_agent_id", "")
    monkeypatch.setattr("agentflow.config.settings.enable_decomposer", False)
    monkeypatch.setattr("agentflow.config.settings.enable_checkpoints", False)
    monkeypatch.setattr("agentflow.orchestrator.engine.stream_registry", StreamRegistry())
    monkeypatch.setattr("agentflow.orchestrator.engine.context_store", ContextStore())
    monkeypatch.setattr("agentflow.orchestrator.engine.task_bus", TaskBus())
    monkeypatch.setattr("agentflow.tools.sandbox.make_sandbox", _RecordingSandbox)
    _RecordingSandbox.instances = []
    eng = OrchestratorEngine(AgentRegistry())
    eng._generate_run_name = AsyncMock(return_value="n")
    return eng


async def _run(eng, execs: int) -> None:
    eng._agent_instances = {"A": _ExecAgent(execs)}
    plan = ExecutionPlan(run_id="r", subtasks=[Subtask(id="s", agent_id="A", instruction="i")])
    with patch("agentflow.orchestrator.engine.compile_report", AsyncMock(return_value="r")):
        await eng.run("r", "t", {}, plan=plan)


async def test_run_reuses_one_sandbox_and_closes_it(engine, ws):
    await _run(engine, execs=3)
    (sb,) = _RecordingSandbox.instances
    assert sb.calls == 3 and sb.closed
    assert sb.workspace == (ws / "runs" / "r").resolve() and sb.run_id == "r"
    assert _current_sandbox.get() is None


async def test_run_without_exec_never_creates_a_sandbox(engine):
    await _run(engine, execs=0)
    assert _RecordingSandbox.instances == []


async def test_calls_outside_a_run_use_a_sandbox_per_command(ws, monkeypatch):
    monkeypatch.setattr("agentflow.tools.sandbox.make_sandbox", _RecordingSandbox)
    _RecordingSandbox.instances = []
    await tool_registry.execute("bash_exec", {"command": "true", "purpose": "t"})
    await tool_registry.execute("bash_exec", {"command": "true", "purpose": "t"})
    assert len(_RecordingSandbox.instances) == 2 and all(s.closed for s in _RecordingSandbox.instances)


def test_provider_is_lazy(ws):
    provider = SandboxProvider(ws, "r")
    assert provider._sandbox is None
    assert isinstance(provider.get(), LocalSandbox) and provider.get() is provider.get()
