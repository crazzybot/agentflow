"""Tests for the planner — cost-tier/thinking-effort parsing and validation."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agentflow.core.models import AgentResult, AgentOutput, AgentStatus
from agentflow.orchestrator.planner import (
    _validate_choice,
    _VALID_MODEL_TIERS,
    _VALID_THINKING_EFFORTS,
    create_plan,
)

# Agent is imported at module scope in planner.py, so patch it there.
_AGENT_PATCH = "agentflow.orchestrator.planner.Agent"


# ---------------------------------------------------------------------------
# _validate_choice — pure helper
# ---------------------------------------------------------------------------

def test_validate_choice_accepts_known_value():
    assert _validate_choice("economy", _VALID_MODEL_TIERS, "modelTier", "st_1") == "economy"


def test_validate_choice_rejects_unknown_value():
    assert _validate_choice("ultra", _VALID_MODEL_TIERS, "modelTier", "st_1") is None


def test_validate_choice_passes_through_none():
    assert _validate_choice(None, _VALID_THINKING_EFFORTS, "thinkingEffort", "st_1") is None


# ---------------------------------------------------------------------------
# create_plan — end-to-end parsing of modelTier / thinkingEffort per subtask
# ---------------------------------------------------------------------------

def _mock_plan_result(subtasks_json: str) -> AgentResult:
    import json
    return AgentResult(
        task_id="task-test",
        agent_id="planner",
        status=AgentStatus.success,
        output=AgentOutput(structured=json.loads(subtasks_json), text=""),
    )


@pytest.mark.asyncio
async def test_create_plan_parses_valid_tier_and_effort():
    plan_json = """
    {"subtasks": [
        {"id": "st_1", "agentId": "CodeAgent", "instruction": "Fix formatting", "dependsOn": [],
         "modelTier": "economy", "thinkingEffort": "low"},
        {"id": "st_2", "agentId": "CodeAgent", "instruction": "Design the schema", "dependsOn": []}
    ]}
    """
    with patch(_AGENT_PATCH) as MockAgent:
        MockAgent.return_value.run = AsyncMock(return_value=_mock_plan_result(plan_json))
        registry = MagicMock()
        registry.summary.return_value = "CodeAgent: ..."

        plan = await create_plan("run-1", "do the thing", registry, MagicMock(), MagicMock())

    assert plan.subtasks[0].model_tier == "economy"
    assert plan.subtasks[0].thinking_effort == "low"
    # Second subtask omitted both fields — they must default to None, not inherit the first's.
    assert plan.subtasks[1].model_tier is None
    assert plan.subtasks[1].thinking_effort is None


@pytest.mark.asyncio
async def test_create_plan_drops_invalid_tier_and_effort():
    """Malformed modelTier/thinkingEffort values are dropped, not fatal to the plan."""
    plan_json = """
    {"subtasks": [
        {"id": "st_1", "agentId": "CodeAgent", "instruction": "Do it", "dependsOn": [],
         "modelTier": "ultra-premium", "thinkingEffort": "extreme"}
    ]}
    """
    with patch(_AGENT_PATCH) as MockAgent:
        MockAgent.return_value.run = AsyncMock(return_value=_mock_plan_result(plan_json))
        registry = MagicMock()
        registry.summary.return_value = "CodeAgent: ..."

        plan = await create_plan("run-1", "do the thing", registry, MagicMock(), MagicMock())

    assert plan.subtasks[0].model_tier is None
    assert plan.subtasks[0].thinking_effort is None
