"""Tests for the generic Agent class (without live LLM or MCP calls)."""
import pytest
from unittest.mock import AsyncMock, MagicMock, call

from anthropic.types import TextBlock, ThinkingBlock

from agentflow.agents.agent import (
    Agent,
    _format_upstream_context,
    _parse_final_output,
    _UPSTREAM_SUMMARY_INLINE_THRESHOLD,
    _with_message_cache_breakpoint,
)
from agentflow.config import settings
from agentflow.core.models import AgentManifest, AgentStatus, SSEEventType, TaskConstraints, TaskContext, TaskEnvelope


def _make_manifest(
    tools: list[str] | None = None,
    tool_limits: dict | None = None,
    thinking_effort: str | None = None,
) -> AgentManifest:
    return AgentManifest(
        agent_id="TestAgent",
        domain="Testing",
        capabilities=["testing"],
        tools=tools or [],
        mcp_servers=[],
        system_prompt="You are a test agent. Return raw JSON: {\"result\": \"done\"}",
        tool_limits=tool_limits,
        thinking_effort=thinking_effort,
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
# Result-size budget: oversized tool_result spills to disk (Fix 1)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_oversized_tool_result_spills_to_file_with_pointer(tmp_path, monkeypatch):
    """A tool result over its ToolDefinition.max_result_chars is written to
    .tool_output/ and replaced with a pointer — never a blind slice — because
    Agent._call_tool is the single choke point where tool_use_id is available
    for a collision-safe filename."""
    from agentflow.tools.registry import tool_registry

    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path))
    tool_def = tool_registry.get("python_exec")
    monkeypatch.setattr(tool_def, "max_result_chars", 50)

    block = MagicMock()
    block.id = "toolu_overflow1"
    block.name = "python_exec"
    block.input = {"code": "print('x' * 500)", "purpose": "test overflow"}

    emitter = MagicMock()
    emitter.emit = MagicMock()

    agent = Agent(_make_manifest(tools=["python_exec"]), MagicMock())
    result = await agent._call_tool(block, [tool_def], emitter, turn_index=0)

    content = result["content"]
    assert "Full output written to" in content
    assert ".tool_output/python_exec_toolu_overflow1.txt" in content

    spilled = tmp_path / ".tool_output" / "python_exec_toolu_overflow1.txt"
    assert spilled.exists()
    assert "x" * 500 in spilled.read_text()


@pytest.mark.asyncio
async def test_tool_use_input_never_mutated_by_result_capping(tmp_path, monkeypatch):
    """The assistant's own tool_use.input must stay untouched in message history —
    only the tool_result (new info from the environment) may be capped. Editing a
    past tool_use.input retroactively makes the model read its own prior request
    as truncated, which it reads identically to a real mid-generation cutoff and
    reacts to by redundantly retrying the call (see docs/context-optimization-plan.md)."""
    from agentflow.tools.registry import tool_registry

    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path))
    tool_def = tool_registry.get("python_exec")
    monkeypatch.setattr(tool_def, "max_result_chars", 50)

    original_input = {"code": "print('x' * 500)", "purpose": "test overflow"}
    block = MagicMock()
    block.id = "toolu_overflow2"
    block.name = "python_exec"
    block.input = dict(original_input)

    emitter = MagicMock()
    emitter.emit = MagicMock()

    agent = Agent(_make_manifest(tools=["python_exec"]), MagicMock())
    await agent._call_tool(block, [tool_def], emitter, turn_index=0)

    # block.input (what becomes the assistant message's tool_use.input on replay)
    # is exactly what was passed in — capping only ever touches the returned
    # tool_result content, never the call's own arguments.
    assert block.input == original_input


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


# ---------------------------------------------------------------------------
# Extended thinking tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_thinking_config_passed_to_llm():
    """When thinking_effort is set, the create call includes adaptive thinking + effort."""
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=_mock_response())

    agent = Agent(_make_manifest(thinking_effort="medium"), mock_client)
    await agent.run(_make_envelope(), MagicMock())

    call_kwargs = mock_client.messages.create.call_args[1]
    assert call_kwargs["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert call_kwargs["output_config"] == {"effort": "medium"}


@pytest.mark.asyncio
async def test_thinking_never_uses_betas():
    """Adaptive thinking needs no beta header, with or without tools on the manifest."""
    for tools in ([], ["file_read"]):
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_response())

        agent = Agent(_make_manifest(tools=tools, thinking_effort="high"), mock_client)
        await agent.run(_make_envelope(), MagicMock())

        call_kwargs = mock_client.messages.create.call_args[1]
        assert "betas" not in call_kwargs


@pytest.mark.asyncio
async def test_thinking_skipped_when_max_tokens_too_tight():
    """Thinking is skipped for an iteration whose budget-derived max_tokens can't spare headroom."""
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=_mock_response())

    envelope = _make_envelope()
    envelope.constraints.budget_usd = 0.003  # tiny — _budget_to_max_tokens floors to 256

    agent = Agent(_make_manifest(thinking_effort="high"), mock_client)
    await agent.run(envelope, MagicMock())

    call_kwargs = mock_client.messages.create.call_args[1]
    assert call_kwargs["max_tokens"] < 1024
    assert "thinking" not in call_kwargs
    assert "output_config" not in call_kwargs


@pytest.mark.asyncio
async def test_thinking_blocks_emitted_as_thought_events():
    """ThinkingBlock content is emitted as agent:thought SSE events."""
    thinking_block = ThinkingBlock(
        type="thinking",
        thinking="I need to reason step by step about this.",
        signature="sig-abc",
    )
    response = MagicMock()
    response.stop_reason = "end_turn"
    response.content = [thinking_block, TextBlock(type="text", text='{"result": "done"}')]
    response.usage.input_tokens = 100
    response.usage.output_tokens = 60
    response.usage.cache_creation_input_tokens = 0
    response.usage.cache_read_input_tokens = 0

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=response)

    emitter = MagicMock()
    emitter.emit = MagicMock()

    agent = Agent(_make_manifest(thinking_effort="high"), mock_client)
    result = await agent.run(_make_envelope(), emitter)

    assert result.status == AgentStatus.success
    thought_calls = [
        c for c in emitter.emit.call_args_list
        if c[1].get("message") == "I need to reason step by step about this."
    ]
    assert len(thought_calls) == 1


@pytest.mark.asyncio
async def test_thinking_uses_global_default():
    """When manifest has no thinking_effort, the global setting is used."""
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=_mock_response())

    agent = Agent(_make_manifest(), mock_client)
    await agent.run(_make_envelope(), MagicMock())

    call_kwargs = mock_client.messages.create.call_args[1]
    assert call_kwargs["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert call_kwargs["output_config"]["effort"] == settings.agent_thinking_effort


@pytest.mark.asyncio
async def test_thinking_disabled_when_global_empty():
    """When global agent_thinking_effort is "" and manifest has no value, thinking is off."""
    from unittest.mock import patch

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=_mock_response())

    with patch.object(settings, "agent_thinking_effort", ""):
        agent = Agent(_make_manifest(), mock_client)
        await agent.run(_make_envelope(), MagicMock())

    call_kwargs = mock_client.messages.create.call_args[1]
    assert "thinking" not in call_kwargs
    assert "output_config" not in call_kwargs


def test_cache_breakpoint_no_double_marking():
    block = {"type": "tool_result", "tool_use_id": "z", "content": "x", "cache_control": {"type": "ephemeral"}}
    messages = [{"role": "user", "content": [block]}]
    result = _with_message_cache_breakpoint(messages)
    # setdefault should not overwrite existing cache_control
    assert result[0]["content"][-1]["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# _parse_final_output tests
# ---------------------------------------------------------------------------

def test_parse_final_output_pure_json():
    """Direct JSON parse: structured populated, text preserved as-is."""
    payload = '{"code": "x", "language": "Python"}'
    structured, text = _parse_final_output(payload)
    assert structured == {"code": "x", "language": "Python"}
    assert text == payload  # kept for reporter display


def test_parse_final_output_fenced_json():
    """Prose + fenced JSON: structured populated, text is prose only."""
    raw = "All done.\n\n---\n\n```json\n{\"code\": \"x\", \"language\": \"Python\"}\n```"
    structured, text = _parse_final_output(raw)
    assert structured == {"code": "x", "language": "Python"}
    assert text == "All done."


def test_parse_final_output_fenced_no_lang_tag():
    """Fence without 'json' tag still extracted correctly."""
    raw = "Summary.\n\n```\n{\"k\": \"v\"}\n```"
    structured, text = _parse_final_output(raw)
    assert structured == {"k": "v"}
    assert text == "Summary."


def test_parse_final_output_outermost_brace():
    """Fallback: outermost { } extraction when no fence is present."""
    raw = 'Here is the result: {"result": "ok"} — done.'
    structured, text = _parse_final_output(raw)
    assert structured == {"result": "ok"}
    assert text == "Here is the result:"


def test_parse_final_output_no_json():
    """No JSON found: structured is empty, text unchanged."""
    raw = "I could not complete the task."
    structured, text = _parse_final_output(raw)
    assert structured == {}
    assert text == raw


def test_parse_final_output_empty():
    structured, text = _parse_final_output("")
    assert structured == {}
    assert text == ""


# ---------------------------------------------------------------------------
# turn_index / tool_call_id / agent:tool_result event tests
# ---------------------------------------------------------------------------

def _make_tool_use_block(name: str = "file_read", tool_id: str = "toolu_t1", input_: dict | None = None):
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_id
    block.name = name
    block.input = input_ or {}
    return block


def _make_tool_use_resp(block, input_tokens: int = 10, output_tokens: int = 5):
    resp = MagicMock()
    resp.stop_reason = "tool_use"
    resp.content = [block]
    resp.usage.input_tokens = input_tokens
    resp.usage.output_tokens = output_tokens
    resp.usage.cache_creation_input_tokens = 0
    resp.usage.cache_read_input_tokens = 0
    return resp


@pytest.mark.asyncio
async def test_tool_call_event_carries_tool_call_id_and_turn_index():
    """agent:progress for a tool call must include tool_call_id and turn_index."""
    tool_block = _make_tool_use_block("file_read", "toolu_x1")
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(
        side_effect=[_make_tool_use_resp(tool_block), _mock_response()]
    )

    emitter = MagicMock()
    emitter.emit = MagicMock()

    agent = Agent(_make_manifest(tools=["file_read"]), mock_client)
    await agent.run(_make_envelope(), emitter)

    # Find the agent:progress call that announces the tool invocation
    progress_calls = [
        c for c in emitter.emit.call_args_list
        if c.args[0] == SSEEventType.agent_progress
        and "file_read" in c.kwargs.get("message", "")
    ]
    assert progress_calls, "Expected at least one agent:progress for tool call"
    kw = progress_calls[0].kwargs
    assert kw["tool_call_id"] == "toolu_x1"
    assert kw["turn_index"] == 1  # first LLM turn → iteration=1


@pytest.mark.asyncio
async def test_tool_result_event_emitted_with_matching_tool_call_id():
    """agent:tool_result must be emitted after the tool executes, with matching tool_call_id."""
    tool_block = _make_tool_use_block("file_read", "toolu_y2")
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(
        side_effect=[_make_tool_use_resp(tool_block), _mock_response()]
    )

    emitter = MagicMock()
    emitter.emit = MagicMock()

    agent = Agent(_make_manifest(tools=["file_read"]), mock_client)
    await agent.run(_make_envelope(), emitter)

    result_calls = [
        c for c in emitter.emit.call_args_list
        if c.args[0] == SSEEventType.agent_tool_result
    ]
    assert result_calls, "Expected agent:tool_result event"
    kw = result_calls[0].kwargs
    assert kw["tool_call_id"] == "toolu_y2"
    assert kw["turn_index"] == 1
    assert "tool" in (kw.get("data") or {})


@pytest.mark.asyncio
async def test_tool_result_event_ordering_relative_to_progress():
    """agent:tool_result must come after its matching agent:progress in the emit sequence."""
    tool_block = _make_tool_use_block("file_read", "toolu_z3")
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(
        side_effect=[_make_tool_use_resp(tool_block), _mock_response()]
    )

    emitter = MagicMock()
    emitter.emit = MagicMock()

    agent = Agent(_make_manifest(tools=["file_read"]), mock_client)
    await agent.run(_make_envelope(), emitter)

    types_in_order = [c.args[0] for c in emitter.emit.call_args_list]
    progress_idx = next(
        (i for i, t in enumerate(types_in_order) if t == SSEEventType.agent_progress and "file_read" in emitter.emit.call_args_list[i].kwargs.get("message", "")),
        None,
    )
    result_idx = next(
        (i for i, t in enumerate(types_in_order) if t == SSEEventType.agent_tool_result),
        None,
    )
    assert progress_idx is not None
    assert result_idx is not None
    assert result_idx > progress_idx


@pytest.mark.asyncio
async def test_budget_exhausted_tool_emits_tool_result_event():
    """When a tool's budget is exhausted, an agent:tool_result event is still emitted."""
    manifest = _make_manifest(tools=["file_read"], tool_limits={"file_read": 1})
    b1 = _make_tool_use_block("file_read", "toolu_a1")
    b2 = _make_tool_use_block("file_read", "toolu_a2")

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(
        side_effect=[_make_tool_use_resp(b1), _make_tool_use_resp(b2), _mock_response()]
    )

    emitter = MagicMock()
    emitter.emit = MagicMock()

    agent = Agent(manifest, mock_client)
    await agent.run(_make_envelope(), emitter)

    result_calls = [
        c for c in emitter.emit.call_args_list
        if c.args[0] == SSEEventType.agent_tool_result
    ]
    # Should have one result for the allowed call and one for the budget-exhausted call
    assert len(result_calls) == 2
    exhausted_call = next(
        (c for c in result_calls if (c.kwargs.get("data") or {}).get("budget_exhausted")),
        None,
    )
    assert exhausted_call is not None
    assert exhausted_call.kwargs["tool_call_id"] == "toolu_a2"


@pytest.mark.asyncio
async def test_turn_index_zero_for_start_event():
    """The initial agent:progress 'Starting:' event must carry turn_index=0."""
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=_mock_response())

    emitter = MagicMock()
    emitter.emit = MagicMock()

    agent = Agent(_make_manifest(), mock_client)
    await agent.run(_make_envelope(), emitter)

    start_calls = [
        c for c in emitter.emit.call_args_list
        if c.kwargs.get("message", "").startswith("Starting:")
    ]
    assert start_calls
    assert start_calls[0].kwargs["turn_index"] == 0


@pytest.mark.asyncio
async def test_turn_index_increments_across_turns():
    """Successive LLM turns produce turn_index values 1, 2, ... on tool call events."""
    b1 = _make_tool_use_block("file_read", "toolu_b1")
    b2 = _make_tool_use_block("file_read", "toolu_b2")

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(
        side_effect=[_make_tool_use_resp(b1), _make_tool_use_resp(b2), _mock_response()]
    )

    emitter = MagicMock()
    emitter.emit = MagicMock()

    agent = Agent(_make_manifest(tools=["file_read"]), mock_client)
    await agent.run(_make_envelope(), emitter)

    result_calls = [
        c for c in emitter.emit.call_args_list
        if c.args[0] == SSEEventType.agent_tool_result
    ]
    assert len(result_calls) == 2
    assert result_calls[0].kwargs["turn_index"] == 1
    assert result_calls[1].kwargs["turn_index"] == 2


# ---------------------------------------------------------------------------
# files_written tracking
# ---------------------------------------------------------------------------

def _make_file_write_tool_block(tool_id: str, path: str):
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_id
    block.name = "file_write"
    block.input = {"path": path, "content": "file content"}
    return block


def _make_file_write_response(tool_id: str, path: str):
    block = _make_file_write_tool_block(tool_id, path)
    resp = MagicMock()
    resp.stop_reason = "tool_use"
    resp.content = [block]
    resp.usage.input_tokens = 10
    resp.usage.output_tokens = 5
    resp.usage.cache_creation_input_tokens = 0
    resp.usage.cache_read_input_tokens = 0
    return resp


@pytest.mark.asyncio
async def test_files_written_populated_on_successful_write():
    """AgentResult.files_written contains paths of files the agent wrote."""
    from unittest.mock import patch

    write_resp = _make_file_write_response("toolu_fw1", "src/main.py")
    end_resp = _mock_response()

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=[write_resp, end_resp])

    emitter = MagicMock()
    emitter.emit = MagicMock()

    with patch("agentflow.tools.tool_registry.execute", new=AsyncMock(return_value="Wrote 1 lines to src/main.py")):
        agent = Agent(_make_manifest(tools=["file_write"]), mock_client)
        result = await agent.run(_make_envelope(), emitter)

    assert result.status == AgentStatus.success
    assert "src/main.py" in result.files_written


@pytest.mark.asyncio
async def test_files_written_empty_when_no_writes():
    """AgentResult.files_written is empty when the agent makes no file_write calls."""
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=_mock_response())

    agent = Agent(_make_manifest(), mock_client)
    result = await agent.run(_make_envelope(), emitter=MagicMock())

    assert result.files_written == []


# ---------------------------------------------------------------------------
# upstream_artifacts context injection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upstream_artifacts_included_in_initial_message():
    """When upstream_artifacts is set, the initial user message contains an upstream_context block."""
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=_mock_response())

    emitter = MagicMock()
    emitter.emit = MagicMock()

    envelope = TaskEnvelope(
        parent_run_id="run-1",
        agent_id="TestAgent",
        instruction="Aggregate results",
        context=TaskContext(
            prior_results={"st_1_a": "Wrote models"},
            upstream_artifacts={"st_1_a": ["src/models.py"]},
        ),
        constraints=TaskConstraints(),
    )

    agent = Agent(_make_manifest(), mock_client)
    await agent.run(envelope, emitter)

    call_messages = mock_client.messages.create.call_args[1]["messages"]
    # _with_message_cache_breakpoint wraps string content in a list block.
    raw = call_messages[0]["content"]
    initial_text = raw if isinstance(raw, str) else next(
        (b["text"] for b in raw if isinstance(b, dict) and b.get("type") == "text"), ""
    )
    assert "<upstream_context>" in initial_text
    assert "src/models.py" in initial_text
    assert "Wrote models" in initial_text


# ---------------------------------------------------------------------------
# Upstream-context pointer-only injection for oversized summaries (Fix 2)
# ---------------------------------------------------------------------------

def test_small_upstream_summary_inlined_in_full():
    """Summaries at or under the threshold are inlined verbatim — no more silent
    hard-clipping at a fixed 500 chars regardless of actual size."""
    summary = "x" * (_UPSTREAM_SUMMARY_INLINE_THRESHOLD - 1)
    block = _format_upstream_context({"st_1": summary}, {})
    assert summary in block
    assert "Full output written to" not in block


def test_oversized_upstream_summary_spills_to_file_with_pointer(tmp_path, monkeypatch):
    """A summary over the threshold is spilled to disk via the same
    write_overflow_file() mechanism as oversized tool results, instead of being
    silently sliced with no indication anything was cut — this is the bug Fix 2
    replaces (the old code did str(summary)[:500] with no marker at all)."""
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path))

    summary = "A" * 3_000 + "B" * 3_000  # well over the threshold
    block = _format_upstream_context({"st_1": summary}, {})

    assert "Full output written to" in block
    assert ".tool_output/upstream_result_st_1.txt" in block
    assert "A" * 100 in block  # head preview present
    assert "B" * 100 in block  # tail preview present — not head-only

    spilled = tmp_path / ".tool_output" / "upstream_result_st_1.txt"
    assert spilled.exists()
    assert spilled.read_text() == summary


def test_upstream_context_still_lists_file_paths_alongside_spilled_summary(tmp_path, monkeypatch):
    """Files already written by an upstream agent were always passed as paths,
    never inlined — Fix 2 only changes the free-text summary side."""
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path))

    summary = "A" * 5_000
    block = _format_upstream_context({"st_1": summary}, {"st_1": ["report.md"]})

    assert "Files written:" in block
    assert "report.md" in block


@pytest.mark.asyncio
async def test_no_prior_messages_injection():
    """Agents always start with a fresh message thread; prior conversation history is never injected."""
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=_mock_response())

    envelope = TaskEnvelope(
        parent_run_id="run-1",
        agent_id="TestAgent",
        instruction="Do something",
        context=TaskContext(),
        constraints=TaskConstraints(),
    )

    agent = Agent(_make_manifest(), mock_client)
    await agent.run(envelope, MagicMock())

    call_messages = mock_client.messages.create.call_args[1]["messages"]
    # Fresh start: only one user message
    assert len(call_messages) == 1
    assert call_messages[0]["role"] == "user"
