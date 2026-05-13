"""Tests for DAG scheduler."""
import pytest
from agentflow.core.models import ExecutionPlan, Subtask
from agentflow.orchestrator.scheduler import DependencyGraph


def _plan(*subtasks: Subtask) -> ExecutionPlan:
    return ExecutionPlan(run_id="test-run", subtasks=list(subtasks))


def test_no_dependencies_all_ready():
    plan = _plan(
        Subtask(id="st_1", agent_id="A", instruction="do A"),
        Subtask(id="st_2", agent_id="B", instruction="do B"),
    )
    graph = DependencyGraph(plan)
    ready = graph.ready(set())
    assert {st.id for st in ready} == {"st_1", "st_2"}


def test_dependency_blocks_downstream():
    plan = _plan(
        Subtask(id="st_1", agent_id="A", instruction="do A"),
        Subtask(id="st_2", agent_id="B", instruction="do B", depends_on=["st_1"]),
    )
    graph = DependencyGraph(plan)
    ready = graph.ready(set())
    assert [st.id for st in ready] == ["st_1"]

    ready2 = graph.ready({"st_1"})
    assert [st.id for st in ready2] == ["st_2"]


def test_cycle_detection():
    plan = _plan(
        Subtask(id="st_1", agent_id="A", instruction="A", depends_on=["st_2"]),
        Subtask(id="st_2", agent_id="B", instruction="B", depends_on=["st_1"]),
    )
    with pytest.raises(ValueError, match="cycle"):
        DependencyGraph(plan)


def test_topological_order():
    plan = _plan(
        Subtask(id="st_1", agent_id="A", instruction="A"),
        Subtask(id="st_2", agent_id="B", instruction="B", depends_on=["st_1"]),
        Subtask(id="st_3", agent_id="C", instruction="C", depends_on=["st_2"]),
    )
    graph = DependencyGraph(plan)
    order = graph.topological_order()
    assert order.index("st_1") < order.index("st_2") < order.index("st_3")
