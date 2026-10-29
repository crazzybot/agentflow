"""Tests for the durable session log: append/read, replay, and run instrumentation."""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from anthropic.types import TextBlock, ToolUseBlock

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
    TaskConstraints,
    TaskContext,
    TaskEnvelope,
)
from agentflow.core.registry import AgentRegistry
from agentflow.core.session import (
    RunSession,
    SessionEventType,
    _current_session,
    read_session,
    record,
    replay,
    session_path,
)
from agentflow.orchestrator.engine import OrchestratorEngine
from agentflow.orchestrator.stream import StreamRegistry

T = SessionEventType


@pytest.fixture
def runs_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "runs"
    monkeypatch.setattr("agentflow.config.settings.runs_dir", str(d))
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path / "workspace"))
    return d


@pytest.fixture
def active_session(runs_dir):
    """Arm the session ContextVar the way engine.run() does, and disarm it after."""
    token = _current_session.set(RunSession("r"))
    yield
    _current_session.reset(token)


# ---------------------------------------------------------------------------
# Append / read
# ---------------------------------------------------------------------------

async def test_append_numbers_events_and_reads_slices(runs_dir):
    s = RunSession("r")
    await s.append(T.run_started, data={"task": "t"})
    await s.append(T.agent_started, invocation_id="i1")
    await s.append(T.message_appended, invocation_id="i1", data={"message": {"role": "user", "content": "x"}})
    await s.append(T.message_appended, invocation_id="i2", data={"message": {"role": "user", "content": "y"}})

    assert [e.seq for e in s.events()] == [1, 2, 3, 4]
    assert [e.seq for e in s.events(after_seq=2)] == [3, 4]
    assert [e.seq for e in s.events(types={T.message_appended})] == [3, 4]
    assert [e.seq for e in s.events(invocation_id="i1")] == [2, 3]


async def test_reopening_continues_numbering(runs_dir):
    await RunSession("r").append(T.run_started)
    reopened = RunSession("r")
    event = await reopened.append(T.run_finished, data={"status": "complete"})
    assert event.seq == 2


async def test_torn_last_line_is_ignored_but_mid_file_corruption_raises(runs_dir):
    s = RunSession("r")
    await s.append(T.run_started)
    with session_path("r").open("a") as f:
        f.write('{"run_id": "r", "seq": 2, "ty')  # killed mid-write
    assert [e.seq for e in read_session("r")] == [1]

    with session_path("r").open("a") as f:
        f.write("\n" + json.dumps({"run_id": "r", "seq": 3, "type": "run_finished"}) + "\n")
    with pytest.raises(ValueError):
        read_session("r")


async def test_record_is_a_noop_outside_a_run(runs_dir):
    await record(T.run_started, data={"task": "t"})
    assert not runs_dir.exists()


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

async def test_replay_folds_log_into_run_state(runs_dir):
    s = RunSession("r")
    plan = ExecutionPlan(run_id="r", subtasks=[Subtask(id="a", agent_id="X", instruction="i")])
    result = AgentResult(task_id="t1", agent_id="X", status=AgentStatus.success, cost_usd=0.5)
    await s.append(T.run_started, data={"task": "do it", "budget_usd": 1.0, "workspace": "runs/r", "user_context": {"k": 1}})
    await s.append(T.plan_created, data={"plan": plan.model_dump(mode="json")})
    await s.append(T.subtask_dispatched, subtask_id="a", data={"task_id": "t1"})
    await s.append(T.agent_started, agent_id="X", invocation_id="i1", data={"task_id": "t1"})
    await s.append(T.message_appended, invocation_id="i1", data={"message": {"role": "user", "content": "go"}})
    await s.append(T.message_appended, invocation_id="i1", data={"message": {"role": "assistant", "content": "cut"}})
    await s.append(T.message_discarded, invocation_id="i1", data={"reason": "max_tokens"})
    await s.append(T.cost_added, data={"cost_usd": 0.5})
    await s.append(T.human_input_received, data={"response": {}, "budget_usd": 2.0})
    await s.append(T.agent_started, agent_id="X", invocation_id="i2", data={"task_id": "t1"})
    await s.append(T.agent_finished, invocation_id="i1", data={"status": "partial"})
    await s.append(T.result_stored, subtask_id="a", data={"result": result.model_dump(mode="json", exclude={"messages"})})

    snap = replay("r", s.events())

    assert snap.task == "do it" and snap.workspace == "runs/r" and snap.user_context == {"k": 1}
    assert snap.plan == plan
    assert snap.dispatched == {"a": "t1"}
    assert snap.results["a"].cost_usd == 0.5
    assert snap.total_cost_usd == 0.5
    assert snap.budget_usd == 2.0  # raised by the human
    assert snap.invocations["i1"].messages == [{"role": "user", "content": "go"}]
    assert snap.invocations["i1"].status == "partial"
    assert [i.invocation_id for i in snap.unfinished_invocations] == ["i2"]
    assert snap.status is None  # no run_finished yet → still in flight
    assert snap.last_seq == 12


# ---------------------------------------------------------------------------
# Agent transcripts
# ---------------------------------------------------------------------------

def _manifest(tools: list[str] | None = None) -> AgentManifest:
    return AgentManifest(
        agent_id="TestAgent", domain="d", capabilities=[], tools=tools or [],
        mcp_servers=[], system_prompt="s",
    )


def _envelope() -> TaskEnvelope:
    return TaskEnvelope(
        parent_run_id="r", agent_id="TestAgent", instruction="Do a test",
        context=TaskContext(), constraints=TaskConstraints(),
    )


def _response(stop_reason: str, content: list) -> MagicMock:
    resp = MagicMock()
    resp.stop_reason = stop_reason
    resp.content = content
    resp.usage.input_tokens = 100
    resp.usage.output_tokens = 50
    resp.usage.cache_creation_input_tokens = 0
    resp.usage.cache_read_input_tokens = 0
    return resp


async def test_agent_transcript_replays_to_its_message_history(active_session):
    tool_use = ToolUseBlock(type="tool_use", id="toolu_1", name="echo", input={"x": 1})
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=[
        _response("tool_use", [tool_use]),
        _response("end_turn", [TextBlock(type="text", text='{"result": "done"}')]),
    ])
    agent = Agent(_manifest(), client)
    agent._call_tool = AsyncMock(return_value={"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"})

    result = await agent.run(_envelope(), MagicMock())

    events = read_session("r")
    snap = replay("r", events)
    (inv,) = snap.invocations.values()
    assert inv.finished and inv.status == AgentStatus.success
    assert inv.agent_id == "TestAgent" and inv.task_id == result.task_id
    assert inv.messages == result.messages
    assistant = [e for e in events if e.type == T.message_appended and e.data["message"]["role"] == "assistant"]
    assert assistant[0].data["stop_reason"] == "tool_use"
    assert assistant[0].data["usage"]["input_tokens"] == 100


async def test_max_tokens_discard_keeps_transcript_in_sync(active_session):
    cut = ToolUseBlock(type="tool_use", id="toolu_cut", name="bash_exec", input={"command": "rm -rf buil"})
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=_response("max_tokens", [cut]))
    agent = Agent(_manifest(), client)

    result = await agent.run(_envelope(), MagicMock())

    events = read_session("r")
    assert any(e.type == T.message_discarded for e in events)
    (inv,) = replay("r", events).invocations.values()
    assert inv.messages == result.messages
    assert result.hit_max_tokens


async def test_resumed_invocation_logs_its_inherited_prefix(active_session):
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=_response("end_turn", [TextBlock(type="text", text="{}")]))
    agent = Agent(_manifest(), client)
    prior = [{"role": "user", "content": "start"}, {"role": "assistant", "content": [{"type": "text", "text": "mid"}]}]

    result = await agent.run(_envelope(), MagicMock(), resume_messages=prior)

    (inv,) = replay("r", read_session("r")).invocations.values()
    assert inv.messages == result.messages
    assert inv.messages[:2] == prior
    started = next(e for e in read_session("r") if e.type == T.agent_started)
    assert started.data["resumed"] is True


# ---------------------------------------------------------------------------
# Engine: the log alone reproduces the run's results and cost
# ---------------------------------------------------------------------------

@pytest.fixture
def engine(runs_dir, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.capture_events", False)
    monkeypatch.setattr("agentflow.config.settings.capture_results", False)
    monkeypatch.setattr("agentflow.config.settings.direct_agent_id", "")
    monkeypatch.setattr("agentflow.config.settings.enable_decomposer", False)
    monkeypatch.setattr("agentflow.orchestrator.engine.stream_registry", StreamRegistry())
    store = ContextStore()
    # Keep the context after the run so the test can compare it with the replay.
    monkeypatch.setattr(store, "remove", lambda run_id: None)
    monkeypatch.setattr("agentflow.orchestrator.engine.context_store", store)
    monkeypatch.setattr("agentflow.orchestrator.engine.task_bus", TaskBus())
    eng = OrchestratorEngine(AgentRegistry())
    eng._generate_run_name = AsyncMock(return_value="Test Run")
    return eng, store


def _fake_agent(agent_id: str, cost: float, status: AgentStatus = AgentStatus.success) -> MagicMock:
    agent = MagicMock()

    async def run(envelope, emitter, resume_messages=None, ctx=None):
        return AgentResult(
            task_id=envelope.task_id, agent_id=agent_id, status=status, cost_usd=cost,
            output=AgentOutput(text=f"{agent_id} out"),
        )

    agent.run = run
    return agent


async def test_replayed_session_matches_live_run_context(engine, runs_dir):
    eng, store = engine
    eng._agent_instances = {"A": _fake_agent("A", 0.25), "B": _fake_agent("B", 0.5)}
    plan = ExecutionPlan(run_id="r", subtasks=[
        Subtask(id="s1", agent_id="A", instruction="first"),
        Subtask(id="s2", agent_id="B", instruction="second", depends_on=["s1"]),
    ])

    with (
        patch("agentflow.orchestrator.engine.create_plan", AsyncMock(return_value=plan)),
        patch("agentflow.orchestrator.engine.compile_report", AsyncMock(return_value="report.md")),
    ):
        await eng.run("r", "the task", {"k": "v"}, budget_usd=5.0)

    ctx = store.get("r")
    snap = replay("r", read_session("r"))

    assert snap.status == "complete"
    assert snap.task == "the task" and snap.user_context == {"k": "v"} and snap.budget_usd == 5.0
    assert snap.workspace == "runs/r"
    assert snap.plan == plan
    assert set(snap.dispatched) == {"s1", "s2"}
    live = await ctx.all_results()
    assert {k: v.model_dump(exclude={"messages"}) for k, v in snap.results.items()} == {
        k: v.model_dump(exclude={"messages"}) for k, v in live.items()
    }
    assert snap.total_cost_usd == pytest.approx(ctx.total_cost_usd()) == pytest.approx(0.75)


async def test_failed_run_records_error_status(engine):
    eng, _ = engine
    with patch("agentflow.orchestrator.engine.create_plan", AsyncMock(side_effect=RuntimeError("boom"))):
        await eng.run("r", "t", {})
    assert replay("r", read_session("r")).status == "error"
    assert _current_session.get() is None


async def test_reconcile_marks_session_interrupted(engine, runs_dir):
    eng, _ = engine
    run_dir = runs_dir / "orphan"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text("{}")
    RunSession("orphan").append_sync(T.run_started, data={"task": "t"})

    assert await eng.reconcile_orphaned_runs() == ["orphan"]
    snap = replay("orphan", read_session("orphan"))
    # Interrupted is not terminal: the run stays resumable.
    assert snap.interrupted and snap.status is None and snap.resumable


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

async def test_session_route_filters_events(runs_dir):
    from agentflow.api import routes

    s = RunSession("r")
    await s.append(T.run_started)
    await s.append(T.agent_started, invocation_id="i1")
    await s.append(T.message_appended, invocation_id="i1", data={"message": {}})

    resp = await routes.get_run_session("r", after_seq=1, type=[T.message_appended], invocation_id="i1")

    assert [e.seq for e in resp.events] == [3]


async def test_cancelled_run_records_cancelled_status(engine):
    import asyncio

    eng, _ = engine
    started = asyncio.Event()

    async def hang(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    with patch("agentflow.orchestrator.engine.create_plan", AsyncMock(side_effect=hang)):
        task = asyncio.create_task(eng.run("r", "t", {}))
        await started.wait()
        assert eng.cancel_run("r")
        await task

    assert replay("r", read_session("r")).status == "cancelled"
