"""Tests for independent acceptance-criteria verification and repair."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentflow.core.bus import TaskBus
from agentflow.core.context import ContextStore
from agentflow.core.models import (
    AgentOutput,
    AgentResult,
    AgentStatus,
    CriterionCheck,
    ExecutionPlan,
    Subtask,
    Verification,
)
from agentflow.core.registry import AgentRegistry
from agentflow.core.session import SessionEventType, read_session
from agentflow.orchestrator.engine import OrchestratorEngine
from agentflow.orchestrator.planner import _parse_criteria
from agentflow.orchestrator.stream import StreamRegistry
from agentflow.orchestrator.verifier import _parse_verdict, repair_message, verify_subtask

CRITERIA = ["app.py defines main()", "README.md has a Usage section"]


def _subtask(criteria: list[str] | None = None) -> Subtask:
    return Subtask(id="s1", agent_id="A", instruction="build it",
                   acceptance_criteria=CRITERIA if criteria is None else criteria)


def _result(**kw) -> AgentResult:
    return AgentResult(task_id="t", agent_id="A", status=AgentStatus.success,
                       output=AgentOutput(text="done"), **kw)


# ---------------------------------------------------------------------------
# Verdict parsing and the verifier agent
# ---------------------------------------------------------------------------

def test_parse_verdict_pass_and_fail_keep_planner_wording():
    ok = _parse_verdict(_subtask(), {"checks": [
        {"criterion": "paraphrase", "met": True, "evidence": "line 3"},
        {"criterion": "x", "met": True},
    ], "summary": "all good"})
    assert ok.passed is True and ok.checks[0].criterion == CRITERIA[0] and ok.summary == "all good"

    bad = _parse_verdict(_subtask(), {"checks": [
        {"met": True}, {"met": False, "evidence": "no Usage heading"},
    ]})
    assert bad.passed is False and [c.criterion for c in bad.unmet] == [CRITERIA[1]]


@pytest.mark.parametrize("structured", [
    {},
    {"checks": [{"met": True}]},                     # wrong count
    {"checks": [{"met": "yes"}, {"met": True}]},     # not a bool
    {"checks": "nope"},
])
def test_unusable_verdict_is_inconclusive(structured):
    assert _parse_verdict(_subtask(), structured).passed is None


async def test_verifier_runs_read_only_on_the_configured_tier():
    captured = {}

    class FakeAgent:
        def __init__(self, manifest, client):
            captured["manifest"] = manifest

        async def run(self, envelope, emitter):
            captured["instruction"] = envelope.instruction
            return AgentResult(task_id="v", agent_id="A.verifier", status=AgentStatus.success, cost_usd=0.01,
                               output=AgentOutput(structured={"checks": [{"met": True}, {"met": True}]}))

    with patch("agentflow.orchestrator.verifier.Agent", FakeAgent):
        verdict, vres = await verify_subtask("r", _subtask(), _result(files_written=["app.py"]), MagicMock(), MagicMock())

    m = captured["manifest"]
    assert set(m.tools) == {"file_read", "bash_exec_readonly"}
    assert m.model_tier == "economy"
    assert "app.py" in captured["instruction"] and CRITERIA[1] in captured["instruction"]
    assert verdict.passed is True and vres.cost_usd == 0.01


async def test_verifier_failure_is_inconclusive():
    class Broken:
        def __init__(self, *_a):
            pass

        async def run(self, *_a):
            return AgentResult(task_id="v", agent_id="A.verifier", status=AgentStatus.failed, error="boom")

    with patch("agentflow.orchestrator.verifier.Agent", Broken):
        verdict, _ = await verify_subtask("r", _subtask(), _result(), MagicMock(), MagicMock())
    assert verdict.passed is None and "boom" in verdict.summary


def test_repair_message_lists_only_unmet_findings():
    v = Verification(passed=False, checks=[
        CriterionCheck(criterion="a", met=True), CriterionCheck(criterion="b", met=False, evidence="missing"),
    ])
    msg = repair_message(v)
    assert "- b" in msg and "missing" in msg and "- a" not in msg


def test_planner_criteria_parsing_is_lenient():
    assert _parse_criteria(None, "s") == []
    assert _parse_criteria(["  x ", "", "y"], "s") == ["x", "y"]
    assert _parse_criteria("not a list", "s") == []
    assert _parse_criteria([1, 2], "s") == []
    assert len(_parse_criteria([str(i) for i in range(9)], "s")) == 5


# ---------------------------------------------------------------------------
# Engine gate
# ---------------------------------------------------------------------------

@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.runs_dir", str(tmp_path / "runs"))
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path / "workspace"))
    monkeypatch.setattr("agentflow.config.settings.capture_events", False)
    monkeypatch.setattr("agentflow.config.settings.capture_results", False)
    monkeypatch.setattr("agentflow.config.settings.direct_agent_id", "")
    monkeypatch.setattr("agentflow.config.settings.enable_decomposer", False)
    monkeypatch.setattr("agentflow.config.settings.verification_max_repairs", 1)
    monkeypatch.setattr("agentflow.orchestrator.engine.stream_registry", StreamRegistry())
    store = ContextStore()
    monkeypatch.setattr(store, "remove", lambda run_id: None)
    monkeypatch.setattr("agentflow.orchestrator.engine.context_store", store)
    monkeypatch.setattr("agentflow.orchestrator.engine.task_bus", TaskBus())
    eng = OrchestratorEngine(AgentRegistry())
    eng._generate_run_name = AsyncMock(return_value="n")
    return eng, store


class _Agent:
    """Producing agent: first run writes a.py, repair runs write b.py."""

    def __init__(self, repair_status: AgentStatus = AgentStatus.success) -> None:
        self.runs: list[list | None] = []
        self.repair_status = repair_status

    async def run(self, envelope, emitter, resume_messages=None, ctx=None):
        self.runs.append(resume_messages)
        first = resume_messages is None
        return AgentResult(
            task_id=envelope.task_id, agent_id="A", cost_usd=0.1,
            status=AgentStatus.success if first else self.repair_status,
            output=AgentOutput(text="v1" if first else "v2"),
            files_written=["a.py"] if first else ["b.py"],
            messages=[{"role": "user", "content": "build it"}, {"role": "assistant", "content": "done"}],
            error=None if first or self.repair_status != AgentStatus.failed else "crashed",
        )


def _verdict(passed: bool | None) -> Verification:
    if passed is None:
        return Verification(passed=None, summary="verifier broke")
    return Verification(passed=passed, checks=[
        CriterionCheck(criterion=CRITERIA[0], met=True),
        CriterionCheck(criterion=CRITERIA[1], met=passed, evidence="" if passed else "no Usage heading"),
    ])


async def _run(eng, store, agent, verdicts: list, criteria: list[str] | None = None):
    eng._agent_instances = {"A": agent}
    plan = ExecutionPlan(run_id="r", subtasks=[_subtask(criteria)])
    verifier_cost = AgentResult(task_id="v", agent_id="A.verifier", status=AgentStatus.success, cost_usd=0.01)
    verify = AsyncMock(side_effect=[(v, verifier_cost) for v in verdicts])
    with (
        patch("agentflow.orchestrator.engine.create_plan", AsyncMock(return_value=plan)),
        patch("agentflow.orchestrator.engine.compile_report", AsyncMock(return_value="report.md")),
        patch("agentflow.orchestrator.engine.verify_subtask", verify),
    ):
        await eng.run("r", "t", {})
    ctx = store.get("r")
    return (await ctx.all_results())["s1"], ctx, verify


async def test_passing_verification_accepts_the_result(engine):
    eng, store = engine
    agent = _Agent()
    result, ctx, verify = await _run(eng, store, agent, [_verdict(True)])

    assert result.status == AgentStatus.success and result.verification.passed is True
    assert len(agent.runs) == 1
    assert ctx.total_cost_usd() == pytest.approx(0.11)  # verifier spend is charged
    events = [e for e in read_session("r") if e.type == SessionEventType.subtask_verified]
    assert events[0].data["verification"]["passed"] is True


async def test_failed_check_is_repaired_from_the_agents_own_transcript(engine):
    eng, store = engine
    agent = _Agent()
    result, ctx, _ = await _run(eng, store, agent, [_verdict(False), _verdict(True)])

    assert len(agent.runs) == 2
    repair_turn = agent.runs[1][-1]
    assert repair_turn["role"] == "user" and "no Usage heading" in repair_turn["content"]
    assert agent.runs[1][:-1] == [{"role": "user", "content": "build it"}, {"role": "assistant", "content": "done"}]
    assert result.status == AgentStatus.success and result.output.text == "v2"
    assert result.verification.repairs == 1
    assert result.files_written == ["a.py", "b.py"]
    assert ctx.total_cost_usd() == pytest.approx(0.1 + 0.01 + 0.1 + 0.01)


async def test_still_failing_after_repairs_is_downgraded_to_partial(engine):
    eng, store = engine
    result, _, verify = await _run(eng, store, _Agent(), [_verdict(False), _verdict(False)])

    assert verify.await_count == 2
    assert result.status == AgentStatus.partial
    assert CRITERIA[1] in result.error and result.verification.passed is False


async def test_inconclusive_verification_passes(engine):
    eng, store = engine
    result, _, _ = await _run(eng, store, _Agent(), [_verdict(None)])
    assert result.status == AgentStatus.success and result.verification.passed is None


async def test_failed_repair_keeps_the_earlier_work_as_partial(engine):
    eng, store = engine
    result, _, _ = await _run(eng, store, _Agent(repair_status=AgentStatus.failed), [_verdict(False)])
    assert result.status == AgentStatus.partial and result.output.text == "v1"
    assert "crashed" in result.error


async def test_no_criteria_or_disabled_means_no_verifier(engine, monkeypatch):
    eng, store = engine
    _, _, verify = await _run(eng, store, _Agent(), [], criteria=[])
    verify.assert_not_awaited()

    monkeypatch.setattr("agentflow.config.settings.enable_verifier", False)
    result, _, verify = await _run(eng, store, _Agent(), [])
    verify.assert_not_awaited()
    assert result.verification is None
