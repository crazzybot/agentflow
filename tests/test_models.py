"""Tests for core data models."""
import pytest
from agentflow.core.models import (
    AgentManifest,
    AgentOutput,
    AgentResult,
    AgentStatus,
    ExecutionPlan,
    RunRequest,
    SSEEvent,
    SSEEventType,
    Subtask,
    TaskEnvelope,
)


def test_task_envelope_defaults():
    env = TaskEnvelope(parent_run_id="run-1", agent_id="ResearchAgent", instruction="test")
    assert env.task_id  # auto-generated
    assert env.constraints.max_tokens == 4096


def test_agent_result_serialization():
    result = AgentResult(
        task_id="t1",
        agent_id="ResearchAgent",
        status=AgentStatus.success,
        output=AgentOutput(text="hello", structured={"key": "value"}),
    )
    data = result.model_dump()
    assert data["status"] == "success"
    assert data["output"]["text"] == "hello"


def test_execution_plan_subtasks():
    plan = ExecutionPlan(
        run_id="run-1",
        subtasks=[
            Subtask(id="st_1", agent_id="ResearchAgent", instruction="gather data"),
            Subtask(id="st_2", agent_id="WriterAgent", instruction="write report", depends_on=["st_1"]),
        ],
    )
    assert len(plan.subtasks) == 2
    assert plan.subtasks[1].depends_on == ["st_1"]


def test_sse_event_type_values():
    assert SSEEventType.run_started == "run:started"
    assert SSEEventType.task_dispatched == "task:dispatched"
    assert SSEEventType.run_complete == "run:complete"


def test_run_request():
    req = RunRequest(task="analyze EV market")
    assert req.task == "analyze EV market"
    assert req.context == {}
