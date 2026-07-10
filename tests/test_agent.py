"""Tests for the generic Agent class (without live LLM or MCP calls)."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from anthropic.types import TextBlock

from agentflow.agents.agent import Agent, _with_message_cache_breakpoint
from agentflow.core.models import AgentManifest, AgentStatus, TaskConstraints, TaskContext, TaskEnvelope


def _make_manifest(tools: list[str] | None = None, tool_limits: dict | None = None) -> AgentManifest:
    return AgentManifest(
        agent_id="TestAgent",
        domain="Testing",
        capabilities=["testing"],
        tools=tools or [],
        mcp_servers=[],
        system_prompt="You are a test agent. Return raw JSON: {\"result\": \"done\"}",
        tool_limits=tool_limits,
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
# tool_limits enforcement tests
# ---------------------------------------------------------------------------

def _make_tool_use_response(tool_name: str, tool_id: str = "toolu_t1"):
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_id
    block.name = tool_name
    block.input = {}
    resp = MagicMock()
    resp.stop_reason = "tool_use"
    resp.content = [block]
    resp.usage.input_tokens = 10
    resp.usage.output_tokens = 5
    resp.usage.cache_creation_input_tokens = 0
    resp.usage.cache_read_input_tokens = 0
    return resp


@pytest.mark.asyncio
async def test_tool_limits_blocks_over_budget():
    """After exhausting a tool's budget the agent receives an error result, not a real call."""
    # Manifest allows python_exec but limits it to 1 call
    manifest = _make_manifest(tools=["python_exec"], tool_limits={"python_exec": 1})

    # LLM asks for the tool twice, then terminates
    resp1 = _make_tool_use_response("python_exec", "toolu_1")
    resp2 = _make_tool_use_response("python_exec", "toolu_2")
    end = _mock_response()

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=[resp1, resp2, end])

    emitter = MagicMock()
    emitter.emit = MagicMock()

    agent = Agent(manifest, mock_client)
    result = await agent.run(_make_envelope(), emitter)

    assert result.status == AgentStatus.success
    # Three LLM calls: tool_use → tool_use → end_turn
    assert mock_client.messages.create.call_count == 3

    # Inspect the messages sent on the second tool-use round-trip: the tool
    # result content for toolu_2 must contain the budget-exhausted message.
    second_call_messages = mock_client.messages.create.call_args_list[2][1]["messages"]
    # The last user message before the final LLM call contains the tool result for toolu_2
    last_user = next(
        m for m in reversed(second_call_messages) if m.get("role") == "user"
    )
    tool_results = last_user["content"]
    over_budget_result = next(
        (r for r in tool_results if r.get("tool_use_id") == "toolu_2"), None
    )
    assert over_budget_result is not None
    assert "budget exhausted" in over_budget_result["content"].lower()


@pytest.mark.asyncio
async def test_tool_limits_allows_calls_within_budget():
    """Calls within the limit go through normally."""
    manifest = _make_manifest(tools=["python_exec"], tool_limits={"python_exec": 2})

    resp1 = _make_tool_use_response("python_exec", "toolu_1")
    resp2 = _make_tool_use_response("python_exec", "toolu_2")
    end = _mock_response()

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=[resp1, resp2, end])

    emitter = MagicMock()
    emitter.emit = MagicMock()

    agent = Agent(manifest, mock_client)
    result = await agent.run(_make_envelope(), emitter)

    assert result.status == AgentStatus.success
    assert mock_client.messages.create.call_count == 3


@pytest.mark.asyncio
async def test_tool_limits_none_means_no_limit():
    """tool_limits=None (default) imposes no restrictions."""
    manifest = _make_manifest(tools=["python_exec"], tool_limits=None)

    resps = [_make_tool_use_response("python_exec", f"toolu_{i}") for i in range(3)]
    end = _mock_response()

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=[*resps, end])

    emitter = MagicMock()
    emitter.emit = MagicMock()

    agent = Agent(manifest, mock_client)
    result = await agent.run(_make_envelope(), emitter)

    assert result.status == AgentStatus.success
    assert mock_client.messages.create.call_count == 4


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
