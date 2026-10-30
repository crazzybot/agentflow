"""Tests for OrchestratorEngine run routing (LLM and planner fully mocked)."""
from unittest.mock import AsyncMock, patch

import pytest

from agentflow.core.registry import AgentRegistry
from agentflow.orchestrator.engine import OrchestratorEngine


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.runs_dir", str(tmp_path / "runs"))
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path / "workspace"))
    monkeypatch.setattr("agentflow.config.settings.capture_events", False)
    monkeypatch.setattr("agentflow.config.settings.capture_results", False)
    eng = OrchestratorEngine(AgentRegistry())
    eng._generate_run_name = AsyncMock(return_value="Test Run")
    eng._is_single_agent_task = AsyncMock(return_value=True)
    return eng


@pytest.mark.asyncio
async def test_without_direct_agent_id_classifier_is_skipped_and_planner_runs(engine, monkeypatch):
    """Unset DIRECT_AGENT_ID used to leave the classifier on — and it defaults to
    "direct" when unsure — so most runs on a fresh install failed with run:error."""
    monkeypatch.setattr("agentflow.config.settings.direct_agent_id", "")
    with patch(
        "agentflow.orchestrator.engine.create_plan",
        AsyncMock(side_effect=RuntimeError("planner reached")),
    ) as create_plan:
        await engine.run("run-a", "do something", {})

    engine._is_single_agent_task.assert_not_awaited()
    create_plan.assert_awaited_once()


@pytest.mark.asyncio
async def test_with_direct_agent_id_classifier_decides_routing(engine, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.direct_agent_id", "CodeAgent")
    with patch("agentflow.orchestrator.engine.create_plan", AsyncMock()) as create_plan:
        # CodeAgent is not registered in this empty registry, so the direct plan
        # raises and the run ends — enough to prove the classifier routed it.
        await engine.run("run-b", "do something", {})

    engine._is_single_agent_task.assert_awaited_once()
    create_plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_continue_partial_skips_max_tokens_result_without_progress(engine):
    """A subtask truncated by max_tokens before any tool turn ran would just replay the
    same history — the engine must not spend continuations on it."""
    from unittest.mock import MagicMock

    from agentflow.core.models import AgentResult, AgentStatus

    stuck = AgentResult(task_id="t", agent_id="CodeAgent", status=AgentStatus.partial, hit_max_tokens=True)
    engine._invoke = AsyncMock()
    subtask = MagicMock(id="feature", agent_id="CodeAgent")

    result = await engine._continue_partial("run-c", subtask, MagicMock(), stuck, MagicMock(), MagicMock())

    assert result is stuck
    engine._invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_continue_partial_resumes_max_tokens_result_that_made_progress(engine):
    from unittest.mock import MagicMock

    from agentflow.core.models import AgentResult, AgentStatus

    progressed = AgentResult(
        task_id="t", agent_id="CodeAgent", status=AgentStatus.partial, hit_max_tokens=True, tool_turns=4,
    )
    done = AgentResult(task_id="t", agent_id="CodeAgent", status=AgentStatus.success)
    engine._invoke = AsyncMock(return_value=done)
    ctx = MagicMock()
    ctx.within_budget.return_value = True
    subtask = MagicMock(id="feature", agent_id="CodeAgent")

    with patch("agentflow.orchestrator.engine._add_cost", AsyncMock()):
        result = await engine._continue_partial("run-d", subtask, MagicMock(), progressed, ctx, MagicMock())

    assert result is done
    engine._invoke.assert_awaited_once()
