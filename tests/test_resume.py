"""Tests for resuming interrupted runs from their session logs (engine.wake)."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from anthropic.types import TextBlock, ToolUseBlock
from fastapi import HTTPException

from agentflow.agents.agent import Agent
from agentflow.core.bus import TaskBus
from agentflow.core.context import ContextStore
from agentflow.core.models import (
    AgentManifest,
    AgentOutput,
    AgentResult,
    AgentStatus,
    ExecutionPlan,
    Subtask,
)
from agentflow.core.registry import AgentRegistry
from agentflow.core.session import (
    Invocation,
    RunSession,
    RunSnapshot,
    SessionEventType,
    read_session,
    replay,
    session_path,
)
from agentflow.orchestrator.engine import OrchestratorEngine, RunAlreadyActiveError
from agentflow.orchestrator.resume import (
    ResumeState,
    RunNotResumableError,
    _trim_transcript,
    load_resumable,
)
from agentflow.orchestrator.stream import StreamRegistry

T = SessionEventType


# ---------------------------------------------------------------------------
# Transcript trimming and resume-state construction
# ---------------------------------------------------------------------------

_USER = {"role": "user", "content": "go"}
_ASK = {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]}
_RESULTS = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}
_ASK2 = {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "x", "input": {}}]}
_TEXT = {"role": "assistant", "content": [{"type": "text", "text": "thinking aloud"}]}


def test_trim_drops_unanswered_tool_request():
    assert _trim_transcript([_USER, _ASK, _RESULTS, _ASK2]) == [_USER, _ASK, _RESULTS]


def test_trim_keeps_answered_history_and_text_turns():
    assert _trim_transcript([_USER, _ASK, _RESULTS]) == [_USER, _ASK, _RESULTS]
    assert _trim_transcript([_USER, _TEXT]) == [_USER, _TEXT]


def test_trim_returns_none_when_nothing_beyond_the_instruction_survives():
    assert _trim_transcript([_USER, _ASK]) is None
    assert _trim_transcript([]) is None


def _snapshot(**kw) -> RunSnapshot:
    return RunSnapshot(run_id="r", task="t", **kw)


def test_resume_state_takes_newest_unfinished_invocation_once():
    snap = _snapshot(
        dispatched={"a": "task-a", "b": "task-b"},
        finished={"b": True},
        invocations={
            "old": Invocation("old", "A", "task-a", [_USER, _TEXT], finished=True),
            "retry": Invocation("retry", "A", "task-a", [_USER, _ASK, _RESULTS]),
            "b1": Invocation("b1", "B", "task-b", [_USER, _TEXT]),
        },
    )
    state = ResumeState.from_snapshot(snap)

    assert set(state.transcripts) == {"a"}  # b already finished
    assert state.take_transcript("a", "A") == [_USER, _ASK, _RESULTS]
    assert state.take_transcript("a", "A") is None  # consumed


def test_resume_state_discards_transcript_from_a_different_agent():
    snap = _snapshot(
        dispatched={"a": "task-a"},
        invocations={"fb": Invocation("fb", "Fallback", "task-a", [_USER, _TEXT])},
    )
    assert ResumeState.from_snapshot(snap).take_transcript("a", "A") is None


# ---------------------------------------------------------------------------
# Engine fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def runs_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "runs"
    monkeypatch.setattr("agentflow.config.settings.runs_dir", str(d))
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path / "workspace"))
    return d


@pytest.fixture
def engine(runs_dir, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.capture_events", False)
    monkeypatch.setattr("agentflow.config.settings.capture_results", False)
    monkeypatch.setattr("agentflow.config.settings.direct_agent_id", "")
    monkeypatch.setattr("agentflow.config.settings.enable_decomposer", False)
    monkeypatch.setattr("agentflow.config.settings.task_max_retries", 1)
    monkeypatch.setattr("agentflow.orchestrator.engine.stream_registry", StreamRegistry())
    store = ContextStore()
    monkeypatch.setattr(store, "remove", lambda run_id: None)  # inspect ctx after the run
    monkeypatch.setattr("agentflow.orchestrator.engine.context_store", store)
    monkeypatch.setattr("agentflow.orchestrator.engine.task_bus", TaskBus())
    eng = OrchestratorEngine(AgentRegistry())
    eng._generate_run_name = AsyncMock(return_value="Test Run")
    return eng, store


class _FakeAgent:
    """Agent stand-in that records how often it ran."""

    def __init__(self, agent_id: str, cost: float) -> None:
        self.agent_id, self.cost, self.calls = agent_id, cost, 0

    async def run(self, envelope, emitter, resume_messages=None, ctx=None):
        self.calls += 1
        return AgentResult(
            task_id=envelope.task_id, agent_id=self.agent_id, status=AgentStatus.success,
            cost_usd=self.cost, output=AgentOutput(text=f"{self.agent_id} out"),
        )


def _response(stop_reason: str, content: list) -> MagicMock:
    resp = MagicMock()
    resp.stop_reason = stop_reason
    resp.content = content
    resp.usage.input_tokens = 10
    resp.usage.output_tokens = 5
    resp.usage.cache_creation_input_tokens = 0
    resp.usage.cache_read_input_tokens = 0
    return resp


def _real_agent(agent_id: str) -> Agent:
    manifest = AgentManifest(
        agent_id=agent_id, domain="d", capabilities=[], tools=[], mcp_servers=[], system_prompt="s",
    )
    return Agent(manifest, MagicMock())


def _plan() -> ExecutionPlan:
    return ExecutionPlan(run_id="r", subtasks=[
        Subtask(id="s1", agent_id="A", instruction="first"),
        Subtask(id="s2", agent_id="B", instruction="second", depends_on=["s1"]),
    ])


def _simulate_crash(run_id: str) -> None:
    """Strip what only a graceful exit writes: a killed process never reaches the
    `finally` that records run_finished."""
    path = session_path(run_id)
    lines = path.read_text().splitlines()
    assert '"run_finished"' in lines[-1]
    path.write_text("\n".join(lines[:-1]) + "\n")


async def _crash_mid_subtask(eng, b_agent: Agent, b_responses: list, hang_in_tool: bool = False):
    """Run the plan until B blocks (in an LLM call or a tool), then 'crash'."""
    blocked = asyncio.Event()

    async def hang(*_a, **_k):
        blocked.set()
        await asyncio.Event().wait()

    if hang_in_tool:
        b_agent.client.messages.create = AsyncMock(side_effect=b_responses)
        tool_results = iter([{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ls output"}])

        async def call_tool(block, *_a, **_k):
            try:
                return next(tool_results)
            except StopIteration:
                return await hang()

        b_agent._call_tool = call_tool
    else:
        # Callables inside a side_effect *list* are returned, not called — sequence
        # the canned responses explicitly, then hang on the next LLM call.
        responses = iter(b_responses)

        async def create(**_kwargs):
            try:
                return next(responses)
            except StopIteration:
                return await hang()

        b_agent.client.messages.create = AsyncMock(side_effect=create)
        b_agent._call_tool = AsyncMock(
            return_value={"type": "tool_result", "tool_use_id": "toolu_1", "content": "ls output"}
        )

    with (
        patch("agentflow.orchestrator.engine.create_plan", AsyncMock(return_value=_plan())),
        patch("agentflow.orchestrator.engine.compile_report", AsyncMock(return_value="report.md")),
    ):
        task = asyncio.create_task(eng.run("r", "the task", {"k": "v"}, budget_usd=5.0))
        await blocked.wait()
        eng.cancel_run("r")
        await task
    _simulate_crash("r")


# ---------------------------------------------------------------------------
# Crash → reconcile → wake
# ---------------------------------------------------------------------------

async def test_wake_skips_finished_work_and_continues_the_interrupted_transcript(engine, runs_dir):
    eng, store = engine
    a, b = _FakeAgent("A", 0.25), _real_agent("B")
    eng._agent_instances = {"A": a, "B": b}
    tool_use = ToolUseBlock(type="tool_use", id="toolu_1", name="bash_exec", input={"command": "ls"})
    await _crash_mid_subtask(eng, b, [_response("tool_use", [tool_use])])

    (runs_dir / "r" / "meta.json").write_text("{}")
    assert eng.reconcile_orphaned_runs() == ["r"]
    assert (runs_dir / "r" / "report.md").read_text().startswith("# Run Interrupted")

    b.client.messages.create = AsyncMock(
        return_value=_response("end_turn", [TextBlock(type="text", text='{"done": true}')])
    )
    with (
        patch("agentflow.orchestrator.engine.create_plan", AsyncMock()) as create_plan,
        patch("agentflow.orchestrator.engine.compile_report", AsyncMock(return_value="report.md")) as report,
    ):
        await eng.wake("r")

    create_plan.assert_not_awaited()  # the logged plan is reused
    assert a.calls == 1  # s1 finished before the crash and is not re-run
    resumed_call = b.client.messages.create.await_args.kwargs["messages"]
    assert [m["role"] for m in resumed_call] == ["user", "assistant", "user"]
    assert resumed_call[2]["content"][0]["content"] == "ls output"  # pre-crash tool result
    report.assert_awaited_once()
    assert not (runs_dir / "r" / "report.md").exists()  # tombstone removed on resume

    snap = replay("r", read_session("r"))
    assert snap.status == "complete" and not snap.interrupted
    assert snap.finished == {"s1": True, "s2": True}
    ctx = store.get("r")
    assert set((await ctx.all_results())) == {"s1", "s2"}
    # Restored cost of s1 plus the resumed s2 (the crashed invocation's spend is not charged).
    assert ctx.total_cost_usd() == pytest.approx(0.25 + snap.results["s2"].cost_usd)
    assert ctx.budget_usd == 5.0


async def test_wake_drops_the_tool_request_that_was_executing_at_the_crash(engine):
    eng, _ = engine
    a, b = _FakeAgent("A", 0.1), _real_agent("B")
    eng._agent_instances = {"A": a, "B": b}
    first = ToolUseBlock(type="tool_use", id="toolu_1", name="bash_exec", input={"command": "ls"})
    second = ToolUseBlock(type="tool_use", id="toolu_2", name="bash_exec", input={"command": "make"})
    await _crash_mid_subtask(
        eng, b, [_response("tool_use", [first]), _response("tool_use", [second])], hang_in_tool=True,
    )

    b.client.messages.create = AsyncMock(return_value=_response("end_turn", [TextBlock(type="text", text="{}")]))
    with patch("agentflow.orchestrator.engine.compile_report", AsyncMock(return_value="report.md")):
        await eng.wake("r")

    messages = b.client.messages.create.await_args.kwargs["messages"]
    assert len(messages) == 3
    assert "toolu_2" not in str(messages)  # unanswered request dropped; the model re-asks


async def test_wake_without_a_logged_plan_plans_again(engine):
    eng, _ = engine
    eng._agent_instances = {"A": _FakeAgent("A", 0.1), "B": _FakeAgent("B", 0.1)}
    s = RunSession("r")
    await s.append(T.run_started, data={"task": "the task", "workspace": "runs/r", "user_context": {}})

    with (
        patch("agentflow.orchestrator.engine.create_plan", AsyncMock(return_value=_plan())) as create_plan,
        patch("agentflow.orchestrator.engine.compile_report", AsyncMock(return_value="report.md")),
    ):
        await eng.wake("r")

    create_plan.assert_awaited_once()
    assert replay("r", read_session("r")).status == "complete"


async def test_wake_reuses_a_logged_micro_plan_without_decomposing(engine, monkeypatch):
    eng, _ = engine
    monkeypatch.setattr("agentflow.config.settings.enable_decomposer", True)
    a = _FakeAgent("A", 0.1)
    eng._agent_instances = {"A": a}
    plan = ExecutionPlan(run_id="r", subtasks=[Subtask(id="s1", agent_id="A", instruction="big")])
    micro = [
        Subtask(id="s1.a", agent_id="A", instruction="part a"),
        Subtask(id="s1.b", agent_id="A", instruction="part b", depends_on=["s1.a"]),
    ]
    s = RunSession("r")
    await s.append(T.run_started, data={"task": "t", "workspace": "runs/r", "user_context": {}})
    await s.append(T.plan_created, data={"plan": plan.model_dump(mode="json")})
    await s.append(T.subtask_decomposed, subtask_id="s1", data={"micro": [m.model_dump(mode="json") for m in micro]})
    await s.append(T.subtask_finished, subtask_id="s1.a", data={"ok": True})
    await s.append(T.result_stored, subtask_id="s1.a", data={"result": AgentResult(
        task_id="x", agent_id="A", status=AgentStatus.success).model_dump(mode="json")})

    with (
        patch("agentflow.orchestrator.engine.decompose_subtask", AsyncMock()) as decompose,
        patch("agentflow.orchestrator.engine.compile_report", AsyncMock(return_value="report.md")),
    ):
        await eng.wake("r")

    decompose.assert_not_awaited()
    assert a.calls == 1  # only s1.b ran
    assert replay("r", read_session("r")).finished == {"s1.a": True, "s1.b": True, "s1": True}


# ---------------------------------------------------------------------------
# What cannot be resumed
# ---------------------------------------------------------------------------

async def test_finished_or_unlogged_runs_are_not_resumable(engine):
    eng, _ = engine
    with pytest.raises(RunNotResumableError, match="no session log"):
        load_resumable("nope")

    s = RunSession("done")
    await s.append(T.run_started, data={"task": "t"})
    await s.append(T.run_finished, data={"status": "complete"})
    with pytest.raises(RunNotResumableError, match="already finished"):
        await eng.wake("done")


async def test_step2_interrupted_status_is_still_resumable(engine):
    s = RunSession("old")
    await s.append(T.run_started, data={"task": "t"})
    await s.append(T.run_finished, data={"status": "interrupted"})
    assert load_resumable("old").resumable


async def test_schedule_resume_refuses_a_run_already_executing(engine):
    eng, _ = engine
    eng._run_tasks["r"] = asyncio.create_task(asyncio.Event().wait())
    try:
        with pytest.raises(RunAlreadyActiveError):
            eng.schedule_resume("r")
    finally:
        eng._run_tasks["r"].cancel()


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def _run_dir(runs_dir: Path, run_id: str) -> Path:
    d = runs_dir / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text("{}")
    return d


async def test_reconcile_skips_finished_runs_and_keeps_real_reports(engine, runs_dir):
    eng, _ = engine
    _run_dir(runs_dir, "finished")
    s = RunSession("finished")
    await s.append(T.run_started, data={"task": "t"})
    await s.append(T.run_finished, data={"status": "complete"})

    # Died after compiling its report but before run_finished.
    d = _run_dir(runs_dir, "late")
    await RunSession("late").append(T.run_started, data={"task": "t"})
    (d / "report.md").write_text("# Real report")

    assert eng.reconcile_orphaned_runs() == ["late"]
    assert (d / "report.md").read_text() == "# Real report"
    # A second restart does not reconcile it again.
    assert eng.reconcile_orphaned_runs() == []


async def test_reconcile_still_tombstones_runs_without_session_logs(engine, runs_dir):
    eng, _ = engine
    d = _run_dir(runs_dir, "legacy")
    assert eng.reconcile_orphaned_runs() == ["legacy"]
    report = (d / "report.md").read_text()
    assert report.startswith("# Run Interrupted") and "resume" not in report


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@pytest.fixture
def routes(runs_dir, monkeypatch):
    from agentflow.api import routes as routes_mod
    fake = MagicMock()
    monkeypatch.setattr(routes_mod, "_get_engine", lambda: fake)
    return routes_mod, fake


async def test_resume_route(routes, runs_dir):
    routes_mod, fake = routes
    with pytest.raises(HTTPException) as exc:
        await routes_mod.resume_run("missing")
    assert exc.value.status_code == 404

    _run_dir(runs_dir, "r")
    fake.schedule_resume.side_effect = RunNotResumableError("already finished")
    with pytest.raises(HTTPException) as exc:
        await routes_mod.resume_run("r")
    assert exc.value.status_code == 409

    fake.schedule_resume.side_effect = None
    with patch.object(routes_mod.stream_registry, "get", return_value=None), \
         patch("agentflow.api.routes.asyncio.sleep", AsyncMock()):
        resp = await routes_mod.resume_run("r")
    assert resp.status == "resumed"
    fake.schedule_resume.assert_called_with("r")


async def test_back_to_back_resume_requests_start_one_run(engine):
    eng, _ = engine
    eng._agent_instances = {"A": _FakeAgent("A", 0.1), "B": _FakeAgent("B", 0.1)}
    s = RunSession("r")
    await s.append(T.run_started, data={"task": "t", "workspace": "runs/r", "user_context": {}})
    await s.append(T.plan_created, data={"plan": _plan().model_dump(mode="json")})

    with patch("agentflow.orchestrator.engine.compile_report", AsyncMock(return_value="report.md")):
        task = eng.schedule_resume("r")
        with pytest.raises(RunAlreadyActiveError):
            eng.schedule_resume("r")
        await task
