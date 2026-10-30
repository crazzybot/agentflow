"""Tests for workspace git checkpoints (shadow repo per run)."""
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agentflow.core.bus import TaskBus
from agentflow.core.context import ContextStore
from agentflow.core.models import AgentResult, AgentStatus, ExecutionPlan, Subtask
from agentflow.core.registry import AgentRegistry
from agentflow.core.session import SessionEventType, read_session
from agentflow.core.workspace import current_workspace
from agentflow.orchestrator.checkpoints import CHECKPOINTS_DIR, WorkspaceCheckpoints
from agentflow.orchestrator.engine import OrchestratorEngine
from agentflow.orchestrator.stream import StreamRegistry

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture
def dirs(tmp_path, monkeypatch) -> tuple[Path, Path]:
    runs, ws = tmp_path / "runs", tmp_path / "workspace"
    monkeypatch.setattr("agentflow.config.settings.runs_dir", str(runs))
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(ws))
    return runs, ws


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


# ---------------------------------------------------------------------------
# WorkspaceCheckpoints
# ---------------------------------------------------------------------------

async def test_commits_snapshot_changes_into_the_run_dir(dirs):
    runs, ws = dirs
    work = ws / "runs" / "r"
    cp = await WorkspaceCheckpoints.open("r", work)
    assert (runs / "r" / CHECKPOINTS_DIR / "HEAD").exists()
    assert await cp.head() is None
    assert await cp.commit("empty") is None  # nothing to snapshot

    (work / "a.txt").write_text("1")
    first = await cp.commit("one")
    assert first.changed == ["a.txt"] and await cp.head() == first.sha

    (work / "a.txt").write_text("2")
    (work / "b.txt").write_text("new")
    second = await cp.commit("two")
    assert sorted(second.changed) == ["a.txt", "b.txt"]
    assert await cp.commit("again") is None


async def test_project_git_repo_and_ignores_are_respected(dirs):
    _, ws = dirs
    project = ws / "proj"
    project.mkdir(parents=True)
    _git(project, "init", "-q")
    (project / ".gitignore").write_text("secret.env\n")
    (project / "app.py").write_text("print(1)")
    _git(project, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(project, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "own")
    own_head = _git(project, "rev-parse", "HEAD")

    (project / "secret.env").write_text("KEY=1")
    (project / "node_modules").mkdir()
    (project / "node_modules" / "big.js").write_text("x")
    cp = await WorkspaceCheckpoints.open("r", project)
    snap = await cp.commit("baseline")

    assert "app.py" in snap.changed
    assert not any(p.startswith((".git/", "node_modules/")) or p == "secret.env" for p in snap.changed)
    assert _git(project, "rev-parse", "HEAD") == own_head  # the project's own repo is untouched


async def test_restore_paths_reverts_edits_and_removes_new_files_only(dirs):
    _, ws = dirs
    work = ws / "w"
    work.mkdir(parents=True)
    (work / "keep.txt").write_text("orig")
    (work / "other.txt").write_text("sibling")
    cp = await WorkspaceCheckpoints.open("r", work)
    base = (await cp.commit("base")).sha

    (work / "keep.txt").write_text("broken")
    (work / "new.txt").write_text("junk")
    (work / "other.txt").write_text("sibling edit")  # another subtask's in-progress work

    restored = await cp.restore_paths(base, ["keep.txt", "new.txt", "../escape.txt", "gone.txt"])

    assert sorted(restored) == ["keep.txt", "new.txt"]
    assert (work / "keep.txt").read_text() == "orig"
    assert not (work / "new.txt").exists()
    assert (work / "other.txt").read_text() == "sibling edit"  # untouched


async def test_restore_without_a_baseline_removes_the_files(dirs):
    _, ws = dirs
    work = ws / "w"
    cp = await WorkspaceCheckpoints.open("r", work)
    (work / "made.txt").write_text("x")
    assert await cp.restore_paths(None, ["made.txt"]) == ["made.txt"]
    assert not (work / "made.txt").exists()


async def test_missing_git_disables_checkpoints(dirs):
    with patch("agentflow.orchestrator.checkpoints.shutil.which", return_value=None):
        assert await WorkspaceCheckpoints.open("r", dirs[1]) is None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

@pytest.fixture
def engine(dirs, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.capture_events", False)
    monkeypatch.setattr("agentflow.config.settings.capture_results", False)
    monkeypatch.setattr("agentflow.config.settings.direct_agent_id", "")
    monkeypatch.setattr("agentflow.config.settings.enable_decomposer", False)
    monkeypatch.setattr("agentflow.orchestrator.engine.stream_registry", StreamRegistry())
    store = ContextStore()
    monkeypatch.setattr(store, "remove", lambda run_id: None)
    monkeypatch.setattr("agentflow.orchestrator.engine.context_store", store)
    monkeypatch.setattr("agentflow.orchestrator.engine.task_bus", TaskBus())
    eng = OrchestratorEngine(AgentRegistry())
    eng._generate_run_name = AsyncMock(return_value="n")
    return eng, store


class _Writer:
    """Writes files into the run workspace; optionally fails its first attempt."""

    def __init__(self, agent_id: str, files: dict[str, str], fail_first_with: dict[str, str] | None = None):
        self.agent_id, self.files, self.fail_first_with = agent_id, files, fail_first_with
        self.attempts: list[dict[str, str | None]] = []

    async def run(self, envelope, emitter, resume_messages=None, ctx=None):
        ws = current_workspace()
        seen = {p: (ws / p).read_text() if (ws / p).exists() else None
                for p in [*self.files, *(self.fail_first_with or {})]}
        self.attempts.append(seen)
        failing = self.fail_first_with is not None and len(self.attempts) == 1
        to_write = self.fail_first_with if failing else self.files
        for rel, text in to_write.items():
            (ws / rel).write_text(text)
        return AgentResult(
            task_id=envelope.task_id, agent_id=self.agent_id,
            status=AgentStatus.failed if failing else AgentStatus.success,
            files_written=list(to_write), error="boom" if failing else None,
        )


async def _run(eng, plan: ExecutionPlan, workspace: str | None = None):
    with (
        patch("agentflow.orchestrator.engine.create_plan", AsyncMock(return_value=plan)),
        patch("agentflow.orchestrator.engine.compile_report", AsyncMock(return_value="report.md")),
        patch("agentflow.orchestrator.engine.asyncio.sleep", AsyncMock()),  # skip retry backoff
    ):
        await eng.run("r", "t", {}, workspace=workspace)


async def test_each_finished_subtask_is_checkpointed(engine, dirs):
    eng, store = engine
    eng._agent_instances = {"A": _Writer("A", {"a.txt": "1"}), "B": _Writer("B", {"b.txt": "2"})}
    plan = ExecutionPlan(run_id="r", subtasks=[
        Subtask(id="s1", agent_id="A", instruction="i"),
        Subtask(id="s2", agent_id="B", instruction="i", depends_on=["s1"]),
    ])
    await _run(eng, plan)

    events = [e for e in read_session("r") if e.type == SessionEventType.checkpoint_created]
    assert [(e.subtask_id, e.data["changed"]) for e in events] == [("s1", ["a.txt"]), ("s2", ["b.txt"])]
    results = await store.get("r").all_results()
    assert results["s1"].checkpoint == events[0].data["sha"]

    from agentflow.api import routes
    listed = (await routes.get_run_checkpoints("r")).checkpoints
    assert [c.subtask_id for c in listed] == ["s1", "s2"] and listed[1].changed == ["b.txt"]


async def test_failed_attempt_is_rolled_back_before_the_retry(engine, dirs, monkeypatch):
    eng, store = engine
    monkeypatch.setattr("agentflow.config.settings.task_max_retries", 2)
    _, ws = dirs
    (ws / "proj").mkdir(parents=True)
    (ws / "proj" / "keep.txt").write_text("orig")
    writer = _Writer("A", {"done.txt": "ok"}, fail_first_with={"keep.txt": "broken", "junk.txt": "x"})
    eng._agent_instances = {"A": writer}
    await _run(eng, ExecutionPlan(run_id="r", subtasks=[Subtask(id="s1", agent_id="A", instruction="i")]),
               workspace="proj")

    # The retry started from the baseline, not from the failed attempt's leftovers.
    assert writer.attempts[1]["keep.txt"] == "orig" and writer.attempts[1]["junk.txt"] is None
    assert not (ws / "proj" / "junk.txt").exists()
    restored = [e for e in read_session("r") if e.type == SessionEventType.workspace_restored]
    assert sorted(restored[0].data["paths"]) == ["junk.txt", "keep.txt"]
    assert (await store.get("r").all_results())["s1"].status == AgentStatus.success


async def test_checkpoints_can_be_disabled(engine, dirs, monkeypatch):
    eng, _ = engine
    monkeypatch.setattr("agentflow.config.settings.enable_checkpoints", False)
    eng._agent_instances = {"A": _Writer("A", {"a.txt": "1"})}
    await _run(eng, ExecutionPlan(run_id="r", subtasks=[Subtask(id="s1", agent_id="A", instruction="i")]))
    assert not (dirs[0] / "r" / CHECKPOINTS_DIR).exists()
