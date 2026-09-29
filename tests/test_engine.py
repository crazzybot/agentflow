"""Tests for OrchestratorEngine run routing (LLM and planner fully mocked)."""
from unittest.mock import AsyncMock, patch

import pytest

from agentflow.core.registry import AgentRegistry
from agentflow.orchestrator.engine import OrchestratorEngine


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.runs_dir", str(tmp_path / "runs"))
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
