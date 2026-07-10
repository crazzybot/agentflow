"""Tests for the generic Agent class (without live LLM or MCP calls)."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from anthropic.types import TextBlock

from agentflow.agents.agent import Agent, _with_message_cache_breakpoint
from agentflow.core.models import AgentManifest, AgentStatus, TaskConstraints, TaskContext, TaskEnvelope


def _make_manifest(tools: list[str] | None = None) -> AgentManifest:
    return AgentManifest(
        agent_id="TestAgent",
        domain="Testing",
        capabilities=["testing"],
        tools=tools or [],
        mcp_servers=[],
        system_prompt="You are a test agent. Return raw JSON: {\"result\": \"done\"}",
    )


def _make_envelope(run_id: str = "run-1") -> TaskEnvelope:
    return TaskEnvelope(
        parent_run_id=run_id,
        agent_id="TestAgent",
        instruction="Do a test",
        context=TaskContext(),
        constraints=TaskConstraints(),
    )


def _mock_response(stop_reason: str = "end_turn", text: str = '{"result": "done"}'):
    block = TextBlock(type="text", text=text)

    response = MagicMock()
    response.stop_reason = stop_reason
    response.content = [block]
    response.usage.input_tokens = 100
    response.usage.output_tokens = 50
    response.usage.cache_creation_input_tokens = 0
    response.usage.cache_read_input_tokens = 0
    return response


@pytest.mark.asyncio
async def test_agent_run_end_turn():
    """Agent returns a successful result when Claude responds with end_turn."""
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=_mock_response())

    emitter = MagicMock()
    emitter.emit = MagicMock()

    agent = Agent(_make_manifest(), mock_client)
    result = await agent.run(_make_envelope(), emitter)

    assert result.status == AgentStatus.success
    assert result.tokens_used == 150
    assert result.output.text == '{"result": "done"}'
    assert result.output.structured == {"result": "done"}


@pytest.mark.asyncio
async def test_agent_executes_tool_and_loops():
    """When Claude returns tool_use, the agent executes the tool and loops."""

    tool_use_block = MagicMock()
    tool_use_block.type = "tool_use"
    tool_use_block.id = "toolu_abc"
    tool_use_block.name = "python_exec"
    tool_use_block.input = {"code": "print('hi')"}

    tool_use_response = MagicMock()
    tool_use_response.stop_reason = "tool_use"
    tool_use_response.content = [tool_use_block]
    tool_use_response.usage.input_tokens = 100
    tool_use_response.usage.output_tokens = 20

    end_turn_response = _mock_response()

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=[tool_use_response, end_turn_response])

    emitter = MagicMock()
    emitter.emit = MagicMock()

    agent = Agent(_make_manifest(tools=["python_exec"]), mock_client)
    result = await agent.run(_make_envelope(), emitter)

    assert result.status == AgentStatus.success
    # create was called twice: once for the tool use, once for the final response
    assert mock_client.messages.create.call_count == 2


@pytest.mark.asyncio
async def test_agent_handles_exception():
    """Unhandled exceptions in the LLM call produce a failed result."""
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=RuntimeError("API down"))

    emitter = MagicMock()
    emitter.emit = MagicMock()

    agent = Agent(_make_manifest(), mock_client)
    result = await agent.run(_make_envelope(), emitter)

    assert result.status == AgentStatus.failed
    assert "API down" in (result.error or "")


@pytest.mark.asyncio
async def test_agent_tool_not_available_returns_error_message():
    """Calling a tool not in the agent's tool list returns an informative error."""
    tool_use_block = MagicMock()
    tool_use_block.type = "tool_use"
    tool_use_block.id = "toolu_xyz"
    tool_use_block.name = "nonexistent_tool"
    tool_use_block.input = {}

    tool_use_response = MagicMock()
    tool_use_response.stop_reason = "tool_use"
    tool_use_response.content = [tool_use_block]
    tool_use_response.usage.input_tokens = 10
    tool_use_response.usage.output_tokens = 5

    end_turn_response = _mock_response()

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=[tool_use_response, end_turn_response])

    emitter = MagicMock()
    emitter.emit = MagicMock()

    # No tools in manifest — the tool should not be available
    agent = Agent(_make_manifest(tools=[]), mock_client)
    result = await agent.run(_make_envelope(), emitter)

    # The loop completes — tool result is an error message, not an exception
    assert result.status == AgentStatus.success


# ---------------------------------------------------------------------------
# _with_message_cache_breakpoint unit tests
# ---------------------------------------------------------------------------

def test_cache_breakpoint_string_content():
    messages = [{"role": "user", "content": "hello"}]
    result = _with_message_cache_breakpoint(messages)
    # String content is promoted to a list block with cache_control
    assert result[0]["content"] == [{"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}]
    # Original is unchanged
    assert messages[0]["content"] == "hello"


def test_cache_breakpoint_list_content():
    messages = [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}
    ]
    result = _with_message_cache_breakpoint(messages)
    assert result[0]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    # Original block is unchanged
    assert "cache_control" not in messages[0]["content"][0]


def test_cache_breakpoint_targets_last_user_message():
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "y", "content": "data"}]},
    ]
    result = _with_message_cache_breakpoint(messages)
    # First user message should be unchanged
    assert result[0]["content"] == "first"
    # Last user message should have cache_control
    assert result[2]["content"][-1].get("cache_control") == {"type": "ephemeral"}


def test_cache_breakpoint_no_double_marking():
    block = {"type": "tool_result", "tool_use_id": "z", "content": "x", "cache_control": {"type": "ephemeral"}}
    messages = [{"role": "user", "content": [block]}]
    result = _with_message_cache_breakpoint(messages)
    # setdefault should not overwrite existing cache_control
    assert result[0]["content"][-1]["cache_control"] == {"type": "ephemeral"}
