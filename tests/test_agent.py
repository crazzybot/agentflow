"""Tests for the generic Agent class (without live LLM or MCP calls)."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agentflow.agents.agent import Agent
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
    block = MagicMock()
    block.type = "text"
    block.text = text

    response = MagicMock()
    response.stop_reason = stop_reason
    response.content = [block]
    response.usage.input_tokens = 100
    response.usage.output_tokens = 50
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
    import agentflow.tools  # ensure built-ins registered

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
