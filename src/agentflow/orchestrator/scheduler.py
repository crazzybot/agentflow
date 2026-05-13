"""DAG-based scheduler — determines execution order and parallelism."""
from __future__ import annotations

import logging
from collections import deque

import networkx as nx

from agentflow.core.models import ExecutionPlan, Subtask

logger = logging.getLogger(__name__)


class DependencyGraph:
    def __init__(self, plan: ExecutionPlan) -> None:
        self._graph: nx.DiGraph = nx.DiGraph()
        self._subtasks: dict[str, Subtask] = {}

        for st in plan.subtasks:
            self._graph.add_node(st.id)
            self._subtasks[st.id] = st

        for st in plan.subtasks:
            for dep in st.depends_on:
                # edge from dep → st (dep must complete before st)
                self._graph.add_edge(dep, st.id)

        if not nx.is_directed_acyclic_graph(self._graph):
            raise ValueError("Execution plan contains a dependency cycle")

    def ready(self, completed: set[str], failed: set[str] = frozenset()) -> list[Subtask]:
        """Return subtasks whose dependencies all succeeded.

        A task is skipped (never returned) if any dependency is in *failed*;
        the caller is responsible for propagating that skip as a cancellation.
        """
        result = []
        for node_id in self._graph.nodes:
            if node_id in completed or node_id in failed:
                continue
            deps = set(self._graph.predecessors(node_id))
            if deps & failed:
                continue  # at least one dependency failed — do not dispatch
            if deps.issubset(completed):
                result.append(self._subtasks[node_id])
        return result

    def topological_order(self) -> list[str]:
        return list(nx.topological_sort(self._graph))
