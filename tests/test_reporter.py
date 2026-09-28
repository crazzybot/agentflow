"""Tests for the final report compiler."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from anthropic.types import TextBlock, ThinkingBlock

from agentflow.config import settings
from agentflow.core.models import AgentOutput, AgentResult, AgentStatus, ExecutionPlan, Subtask
from agentflow.orchestrator import reporter


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        run_id="run-1",
        subtasks=[Subtask(id="st_1", agent_id="WriterAgent", instruction="write", depends_on=[])],
    )


def _result(text: str) -> AgentResult:
    return AgentResult(
        task_id="st_1", agent_id="WriterAgent", status=AgentStatus.success,
        output=AgentOutput(text=text),
    )


def _client(stop_reason: str = "end_turn", content: list | None = None) -> MagicMock:
    response = MagicMock()
    response.stop_reason = stop_reason
    response.content = content or [TextBlock(type="text", text="## Summary\nAll good.")]
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=response)
    return client


@pytest.fixture(autouse=True)
def _runs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "runs_dir", str(tmp_path))


@pytest.mark.asyncio
async def test_report_uses_standard_tier_and_large_max_tokens():
    client = _client()
    with patch.object(settings, "report_model", ""), patch.object(settings, "report_model_tier", "standard"):
        await reporter.compile_report("run-1", "task", _plan(), {"st_1": _result("x")}, client)

    kwargs = client.messages.create.call_args[1]
    assert kwargs["model"] == settings.model_tier_standard
    assert kwargs["max_tokens"] == settings.report_max_tokens


@pytest.mark.asyncio
async def test_report_skips_effort_for_haiku():
    client = _client()
    with patch.object(settings, "report_model", "claude-haiku-4-5-20251001"):
        await reporter.compile_report("run-1", "task", _plan(), {"st_1": _result("x")}, client)

    assert "output_config" not in client.messages.create.call_args[1]


@pytest.mark.asyncio
async def test_long_agent_result_is_not_clipped_at_8k():
    client = _client()
    long_text = "word " * 5_000  # 25k chars — the old cap cut this at 8k
    await reporter.compile_report("run-1", "task", _plan(), {"st_1": _result(long_text)}, client)

    sent = client.messages.create.call_args[1]["messages"][0]["content"]
    assert long_text.strip() in sent
    assert "[truncated]" not in sent


@pytest.mark.asyncio
async def test_truncated_report_is_flagged(tmp_path):
    client = _client(stop_reason="max_tokens")
    path = await reporter.compile_report("run-1", "task", _plan(), {"st_1": _result("x")}, client)

    report = open(path, encoding="utf-8").read()
    assert "All good." in report
    assert "output token limit" in report


@pytest.mark.asyncio
async def test_report_body_skips_thinking_blocks():
    client = _client(content=[
        ThinkingBlock(type="thinking", thinking="planning the report", signature="s"),
        TextBlock(type="text", text="Part one. "),
        TextBlock(type="text", text="Part two."),
    ])
    path = await reporter.compile_report("run-1", "task", _plan(), {"st_1": _result("x")}, client)

    report = open(path, encoding="utf-8").read()
    assert "Part one. Part two." in report
    assert "planning the report" not in report
