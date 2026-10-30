"""Tests for per-run workspace resolution, isolation, and locking."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from agentflow.core.bus import TaskBus
from agentflow.core.context import ContextStore
from agentflow.core.models import FollowUpRequest, RunMeta, RunRequest, SSEEventType
from agentflow.core.registry import AgentRegistry
from agentflow.core.workspace import (
    InvalidWorkspaceError,
    WorkspaceLocks,
    _current_workspace,
    current_workspace,
    run_workspace_rel,
    validate_workspace_name,
    workspace_path,
)
from agentflow.orchestrator.engine import OrchestratorEngine
from agentflow.orchestrator.stream import StreamRegistry
from agentflow.tools.registry import tool_registry


@pytest.fixture
def ws_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "workspace"
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(root))
    return root


# ---------------------------------------------------------------------------
# Name validation and path resolution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["app-gateway", "my_app.v2", "A1"])
def test_valid_workspace_names(name):
    assert validate_workspace_name(name) == name


@pytest.mark.parametrize("name", ["", "..", ".hidden", "a/b", "a\\b", "runs", "-x", "x" * 65])
def test_invalid_workspace_names(name):
    with pytest.raises(InvalidWorkspaceError):
        validate_workspace_name(name)


def test_run_workspace_rel_defaults_to_fresh_per_run_dir():
    assert run_workspace_rel("abc", None) == "runs/abc"
    assert run_workspace_rel("abc", "app-gateway") == "app-gateway"


def test_workspace_path_refuses_escape(ws_root):
    assert workspace_path("runs/abc") == (ws_root / "runs" / "abc").resolve()
    assert workspace_path(".") == ws_root.resolve()
    with pytest.raises(InvalidWorkspaceError):
        workspace_path("../outside")


def test_request_models_reject_bad_workspace_names():
    assert RunRequest(task="t", workspace="proj").workspace == "proj"
    assert RunRequest(task="t").workspace is None
    with pytest.raises(ValidationError):
        RunRequest(task="t", workspace="../etc")
    with pytest.raises(ValidationError):
        FollowUpRequest(task="t", workspace="runs")


# ---------------------------------------------------------------------------
# Tool isolation via the ContextVar
# ---------------------------------------------------------------------------

def test_current_workspace_falls_back_to_root_outside_a_run(ws_root):
    assert current_workspace() == ws_root.resolve()
    assert ws_root.is_dir()


async def test_concurrent_runs_write_into_their_own_workspaces(ws_root):
    async def write_in(rel: str) -> None:
        _current_workspace.set(workspace_path(rel))
        await tool_registry.execute("file_write", {"path": "out.txt", "content": rel})

    # Each create_task copies the current context, so the sets don't interfere.
    await asyncio.gather(
        asyncio.create_task(write_in("runs/a")),
        asyncio.create_task(write_in("runs/b")),
    )

    assert (ws_root / "runs" / "a" / "out.txt").read_text() == "runs/a"
    assert (ws_root / "runs" / "b" / "out.txt").read_text() == "runs/b"
    assert not (ws_root / "out.txt").exists()


async def test_file_read_cannot_reach_a_sibling_run_workspace(ws_root):
    (ws_root / "runs" / "other").mkdir(parents=True)
    (ws_root / "runs" / "other" / "secret.txt").write_text("nope")
    _current_workspace.set(workspace_path("runs/mine"))

    result = await tool_registry.execute("file_read", {"path": "../other/secret.txt"})

    assert "traversal" in result


# ---------------------------------------------------------------------------
# WorkspaceLocks
# ---------------------------------------------------------------------------

async def test_workspace_locks_serialise_same_key_and_clean_up():
    locks = WorkspaceLocks()
    order: list[str] = []

    async def use(key: str, label: str) -> None:
        async with locks.hold(key):
            order.append(f"{label}-in")
            await asyncio.sleep(0.01)
            order.append(f"{label}-out")

    await asyncio.gather(use("p", "a"), use("p", "b"))

    assert order in (["a-in", "a-out", "b-in", "b-out"], ["b-in", "b-out", "a-in", "a-out"])
    assert locks._locks == {} and locks._users == {}


async def test_workspace_locks_do_not_block_different_keys():
    locks = WorkspaceLocks()
    async with locks.hold("p"):
        assert locks.is_busy("p")
        # Would deadlock if different keys shared a lock.
        await asyncio.wait_for(_enter(locks, "q"), timeout=1)


async def _enter(locks: WorkspaceLocks, key: str) -> None:
    async with locks.hold(key):
        pass


# ---------------------------------------------------------------------------
# Engine integration
# ---------------------------------------------------------------------------

@pytest.fixture
def engine(tmp_path, ws_root, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.runs_dir", str(tmp_path / "runs"))
    monkeypatch.setattr("agentflow.config.settings.capture_events", False)
    monkeypatch.setattr("agentflow.config.settings.capture_results", False)
    monkeypatch.setattr("agentflow.config.settings.direct_agent_id", "")
    # In-memory backends regardless of STATE_BACKEND in the developer's .env.
    monkeypatch.setattr("agentflow.orchestrator.engine.stream_registry", StreamRegistry())
    monkeypatch.setattr("agentflow.orchestrator.engine.context_store", ContextStore())
    monkeypatch.setattr("agentflow.orchestrator.engine.task_bus", TaskBus())
    eng = OrchestratorEngine(AgentRegistry())
    eng._generate_run_name = AsyncMock(return_value="Test Run")
    return eng


def _meta(tmp_path: Path, run_id: str) -> RunMeta:
    return RunMeta.model_validate_json((tmp_path / "runs" / run_id / "meta.json").read_text())


async def _planner_writes_marker(*_args, **_kwargs):
    # The planner runs inside the run's context, so its tools see the run workspace.
    await tool_registry.execute("file_write", {"path": "marker.txt", "content": "x"})
    raise RuntimeError("stop after planning")


async def test_run_gets_fresh_workspace_recorded_in_meta(engine, tmp_path, ws_root):
    with patch("agentflow.orchestrator.engine.create_plan", AsyncMock(side_effect=_planner_writes_marker)):
        await engine.run("r1", "task", {})

    assert _meta(tmp_path, "r1").workspace == "runs/r1"
    assert (ws_root / "runs" / "r1" / "marker.txt").exists()
    assert _current_workspace.get() is None


async def test_run_in_named_workspace(engine, tmp_path, ws_root):
    with patch("agentflow.orchestrator.engine.create_plan", AsyncMock(side_effect=_planner_writes_marker)):
        await engine.run("r2", "task", {}, workspace="proj")

    assert _meta(tmp_path, "r2").workspace == "proj"
    assert (ws_root / "proj" / "marker.txt").exists()


async def test_runs_sharing_a_workspace_are_serialised(engine):
    release = asyncio.Event()
    active = 0
    peak = 0

    planning = asyncio.Event()

    async def slow_plan(*_args, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        planning.set()
        await release.wait()
        active -= 1
        raise RuntimeError("done")

    from agentflow.orchestrator import engine as engine_mod

    def r4_waiting() -> bool:
        emitter = engine_mod.stream_registry.get("r4")
        return emitter is not None and any((e.payload.data or {}).get("waiting") for e in emitter._buffer)

    with patch("agentflow.orchestrator.engine.create_plan", AsyncMock(side_effect=slow_plan)):
        first = asyncio.create_task(engine.run("r3", "t", {}, workspace="proj"))
        await asyncio.wait_for(planning.wait(), timeout=5)  # r3 holds the workspace
        second = asyncio.create_task(engine.run("r4", "t", {}, workspace="proj"))
        for _ in range(500):  # until r4 reaches the lock and reports it is waiting
            if r4_waiting():
                break
            await asyncio.sleep(0.01)
        assert active == 1
        release.set()
        await asyncio.gather(first, second)

    assert peak == 1
    events = engine_mod.stream_registry.get("r4")._buffer
    assert any(
        e.type == SSEEventType.agent_progress and (e.payload.data or {}).get("waiting") for e in events
    )


async def test_run_rejects_workspace_escaping_root(engine):
    with pytest.raises(InvalidWorkspaceError):
        await engine.run("r5", "t", {}, workspace="../../etc")


# ---------------------------------------------------------------------------
# Routes: followup inheritance and artifact resolution
# ---------------------------------------------------------------------------

def _write_run(tmp_path: Path, run_id: str, workspace: str | None) -> Path:
    d = tmp_path / "runs" / run_id
    d.mkdir(parents=True)
    meta = {"run_id": run_id, "task": "t", "name": "n", "created_at": "now"}
    if workspace is not None:
        meta["workspace"] = workspace
    (d / "meta.json").write_text(json.dumps(meta))
    return d


@pytest.fixture
def routes(tmp_path, ws_root, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.runs_dir", str(tmp_path / "runs"))
    from agentflow.api import routes as routes_mod
    fake_engine = AsyncMock()
    monkeypatch.setattr(routes_mod, "_get_engine", lambda: fake_engine)
    return routes_mod, fake_engine


@pytest.mark.parametrize(
    ("recorded", "requested", "expected"),
    [
        ("runs/prior", None, "runs/prior"),   # inherit the prior run's workspace
        ("runs/prior", "proj", "proj"),       # explicit name wins
        (None, None, "."),                    # legacy run → workspace root
    ],
)
async def test_followup_workspace_selection(routes, tmp_path, recorded, requested, expected):
    routes_mod, fake_engine = routes
    _write_run(tmp_path, "prior", recorded)

    await routes_mod.followup_run("prior", FollowUpRequest(task="more", workspace=requested))
    await asyncio.sleep(0)  # let the scheduled engine.run coroutine start

    assert fake_engine.run.call_args.args[4] == expected


async def test_followup_rejects_tampered_workspace(routes, tmp_path):
    routes_mod, _ = routes
    _write_run(tmp_path, "prior", "../../etc")

    with pytest.raises(HTTPException) as exc:
        await routes_mod.followup_run("prior", FollowUpRequest(task="more"))
    assert exc.value.status_code == 400


async def test_artifact_content_resolves_against_run_workspace(routes, tmp_path, ws_root):
    routes_mod, _ = routes
    d = _write_run(tmp_path, "r", "runs/r")
    (ws_root / "runs" / "r").mkdir(parents=True)
    (ws_root / "runs" / "r" / "a.md").write_text("hello")
    (d / "artifacts.jsonl").write_text(json.dumps({"id": "x", "name": "a.md", "path": "a.md"}) + "\n")

    resp = await routes_mod.get_run_artifact_content("r", "x")

    assert resp.content == "hello"
