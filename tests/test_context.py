"""Tests for RunContext backends — behaviour that must match across memory and Redis."""
from unittest.mock import MagicMock

import pytest

from agentflow.core.context import RunContext
from agentflow.core.context_redis import RedisRunContext
from agentflow.core.models import AgentOutput, AgentResult, AgentStatus


def _result(text: str, structured: dict, files: list[str] | None = None) -> AgentResult:
    return AgentResult(
        task_id="t",
        agent_id="A",
        status=AgentStatus.success,
        output=AgentOutput(text=text, structured=structured),
        files_written=files or [],
    )


def _contexts() -> list:
    memory = RunContext("run-1")
    redis = RedisRunContext("run-1", MagicMock())
    return [memory, redis]


@pytest.mark.parametrize("ctx", _contexts(), ids=["memory", "redis"])
def test_prior_results_include_text_and_structured_json(ctx):
    """The Redis backend used to return `text or str(structured)`, dropping the JSON
    whenever a prose preamble existed and emitting a Python repr otherwise."""
    results = {
        "st_1": _result("Summary.", {"answer": 42}),
        "st_2": _result("", {"k": "v"}),
        "st_3": _result("prose only", {}),
    }
    store = ctx._results if isinstance(ctx, RunContext) else ctx._local_results
    store.update(results)

    prior = ctx.build_prior_results(["st_1", "st_2", "st_3", "missing"])

    assert prior == {
        "st_1": 'Summary.\n\n{\n  "answer": 42\n}',
        "st_2": '{\n  "k": "v"\n}',
        "st_3": "prose only",
    }


@pytest.mark.parametrize("ctx", _contexts(), ids=["memory", "redis"])
def test_upstream_artifacts_match_across_backends(ctx):
    store = ctx._results if isinstance(ctx, RunContext) else ctx._local_results
    store.update({"st_1": _result("x", {}, ["a.py"]), "st_2": _result("y", {})})
    assert ctx.build_upstream_artifacts(["st_1", "st_2"]) == {"st_1": ["a.py"]}
