"""Tests for read-time context snipping and the recall tool."""
import copy
from unittest.mock import AsyncMock, MagicMock

import pytest
from anthropic.types import TextBlock, ToolUseBlock

from agentflow.agents.agent import Agent
from agentflow.agents.context_view import (
    RECALL_TOOL_NAME,
    ContextSnipper,
    estimate_tokens,
    recall,
)
from agentflow.core.models import AgentManifest, AgentStatus, TaskConstraints, TaskContext, TaskEnvelope
from agentflow.core.session import RunSession, SessionEventType, _current_session, read_session

BIG = "x" * 2_000


def _history(n_turns: int, size: int = 2_000) -> list[dict]:
    """Opening instruction, then n (assistant tool_use, user tool_result) pairs."""
    msgs: list[dict] = [{"role": "user", "content": "do it"}]
    for i in range(n_turns):
        msgs.append({"role": "assistant", "content": [
            {"type": "thinking", "thinking": f"t{i}", "signature": f"sig{i}"},
            {"type": "tool_use", "id": f"tu{i}", "name": "bash_exec", "input": {"command": f"c{i}"}},
        ]})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"tu{i}", "content": f"{i}:" + "x" * size},
        ]})
    return msgs


def _results(view: list[dict]) -> list[str]:
    return [b["content"] for m in view if m["role"] == "user" and isinstance(m["content"], list)
            for b in m["content"] if b.get("type") == "tool_result"]


def _snipper(trigger: int = 100, keep: int = 2, min_chars: int = 500) -> ContextSnipper:
    return ContextSnipper(trigger_tokens=trigger, keep_recent=keep, min_chars=min_chars)


# ---------------------------------------------------------------------------
# ContextSnipper
# ---------------------------------------------------------------------------

def test_no_snip_below_trigger():
    s = _snipper(trigger=10_000)
    msgs = _history(5)
    assert s.maybe_snip(msgs, prompt_tokens=9_000, iteration=5) is None
    assert s.project(msgs) is msgs


def test_snip_stubs_older_results_and_keeps_recent_and_assistant_turns():
    s = _snipper(keep=2)
    msgs = _history(5)
    original = copy.deepcopy(msgs)

    event = s.maybe_snip(msgs, prompt_tokens=5_000, iteration=5)
    view = s.project(msgs)

    assert event is not None and event.snipped == 3 and event.freed_chars > 6_000
    results = _results(view)
    assert all("omitted" in r and f'tool_use_id="tu{i}"' in r for i, r in enumerate(results[:3]))
    assert results[3].startswith("3:") and results[4].startswith("4:")  # recent kept
    assert [m for m in view if m["role"] == "assistant"] == [m for m in msgs if m["role"] == "assistant"]
    assert msgs == original  # history itself never modified


def test_small_results_stay_inline():
    s = _snipper(keep=1, min_chars=5_000)
    msgs = _history(3, size=100)
    assert s.maybe_snip(msgs, prompt_tokens=5_000, iteration=3) is None  # nothing worth stubbing
    assert _results(s.project(msgs))[0].startswith("0:")


def test_view_is_stable_between_snips_and_stale_sizes_are_ignored():
    s = _snipper(keep=1)
    msgs = _history(3)
    assert s.maybe_snip(msgs, prompt_tokens=5_000, iteration=3) is not None
    first = s.project(msgs)

    # Two new turns arrive. The pre-snip measurement (iteration 3) must not re-trigger.
    msgs += _history(2)[1:]
    assert s.maybe_snip(msgs, prompt_tokens=5_000, iteration=3) is None
    # The cached prefix is byte-identical: only new turns were appended.
    assert s.project(msgs)[: len(first)] == first

    # A fresh post-snip measurement that is still too big moves the boundary forward.
    event = s.maybe_snip(msgs, prompt_tokens=5_000, iteration=4)
    assert event is not None and event.boundary > 0
    assert len([r for r in _results(s.project(msgs)) if "omitted" in r]) == 4


def test_nothing_left_to_snip_returns_none():
    s = _snipper(keep=2)
    msgs = _history(2)
    assert s.maybe_snip(msgs, prompt_tokens=5_000, iteration=2) is None


def test_first_call_uses_an_estimate():
    s = _snipper(trigger=1_000, keep=1)
    msgs = _history(4)  # ~8k chars ≈ 2k tokens
    assert estimate_tokens(msgs) > 1_000
    assert s.maybe_snip(msgs, prompt_tokens=None, iteration=0) is not None


def test_disabled_when_trigger_is_zero():
    s = _snipper(trigger=0)
    assert not s.enabled
    assert s.maybe_snip(_history(5), prompt_tokens=10**9, iteration=5) is None


def test_recall_returns_original_content():
    msgs = _history(2)
    assert recall(msgs, "tu1").startswith("1:x")
    assert "No earlier tool result" in recall(msgs, "nope")


# ---------------------------------------------------------------------------
# Agent loop integration
# ---------------------------------------------------------------------------

def _manifest() -> AgentManifest:
    return AgentManifest(
        agent_id="TestAgent", domain="d", capabilities=[], tools=[], mcp_servers=[],
        system_prompt="s", max_iterations=20,
    )


def _envelope() -> TaskEnvelope:
    return TaskEnvelope(
        parent_run_id="r", agent_id="TestAgent", instruction="go",
        context=TaskContext(), constraints=TaskConstraints(),
    )


def _response(stop_reason: str, content: list, input_tokens: int = 5_000) -> MagicMock:
    resp = MagicMock()
    resp.stop_reason = stop_reason
    resp.content = content
    resp.usage.input_tokens = input_tokens
    resp.usage.output_tokens = 10
    resp.usage.cache_creation_input_tokens = 0
    resp.usage.cache_read_input_tokens = 0
    return resp


def _tool_call(i: int, name: str = "bash_exec", **inp) -> MagicMock:
    return _response("tool_use", [ToolUseBlock(type="tool_use", id=f"tu{i}", name=name, input=inp or {"c": i})])


@pytest.fixture
def snip_settings(monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.context_snip_trigger_tokens", 1_000)
    monkeypatch.setattr("agentflow.config.settings.context_snip_keep_recent", 1)
    monkeypatch.setattr("agentflow.config.settings.context_snip_min_chars", 500)


def _agent(responses: list) -> Agent:
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=responses)
    agent = Agent(_manifest(), client)

    async def big_result(block, *_a, **_k):
        return {"type": "tool_result", "tool_use_id": block.id, "content": f"{block.id}:" + BIG}

    agent._call_tool = big_result
    return agent


async def test_agent_sends_snipped_view_but_keeps_full_history(snip_settings, tmp_path, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.runs_dir", str(tmp_path / "runs"))
    token = _current_session.set(RunSession("r"))
    agent = _agent([_tool_call(0), _tool_call(1), _tool_call(2), _response("end_turn", [TextBlock(type="text", text="{}")])])
    try:
        result = await agent.run(_envelope(), MagicMock())
    finally:
        _current_session.reset(token)

    calls = agent.client.messages.create.await_args_list
    last_view = calls[-1].kwargs["messages"]
    first = _results(last_view)[0]
    assert "omitted" in first and 'tool_use_id="tu0"' in first
    assert _results(last_view)[-1].startswith("tu2:")  # most recent intact
    assert any(t["name"] == RECALL_TOOL_NAME for t in calls[-1].kwargs["tools"])
    assert all("omitted" not in r for r in _results(result.messages))  # full history returned
    snips = [e for e in read_session("r") if e.type == SessionEventType.context_snipped]
    assert snips and snips[0].data["snipped"] >= 1
    finished = next(e for e in read_session("r") if e.type == SessionEventType.agent_finished)
    assert finished.data["peak_prompt_tokens"] == result.peak_prompt_tokens == 5_000


async def test_recall_tool_returns_the_omitted_original(snip_settings):
    agent = _agent([
        _tool_call(0), _tool_call(1), _tool_call(2),
        _tool_call(3, name=RECALL_TOOL_NAME, tool_use_id="tu0"),
        _response("end_turn", [TextBlock(type="text", text="{}")]),
    ])
    result = await agent.run(_envelope(), MagicMock())

    assert result.status == AgentStatus.success
    recalled = next(b for m in result.messages if m["role"] == "user" and isinstance(m["content"], list)
                    for b in m["content"] if b.get("tool_use_id") == "tu3")
    assert recalled["content"] == "tu0:" + BIG


async def test_snipping_disabled_sends_full_history(monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.context_snip_trigger_tokens", 0)
    agent = _agent([_tool_call(0), _tool_call(1), _response("end_turn", [TextBlock(type="text", text="{}")])])
    await agent.run(_envelope(), MagicMock())

    last = agent.client.messages.create.await_args_list[-1].kwargs
    assert "omitted" not in str(last["messages"])
    assert all(t["name"] != RECALL_TOOL_NAME for t in last.get("tools", []))
