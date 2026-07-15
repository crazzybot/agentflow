"""Tests for the task decomposer — context block parsing, tuple return, and injection."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agentflow.core.models import AgentManifest, AgentStatus, AgentResult, AgentOutput, Subtask
from agentflow.orchestrator.decomposer import (
    _extract_context_block,
    _strip_context_block,
    _extract_json_array,
    decompose_subtask,
)


# ---------------------------------------------------------------------------
# Unit tests — pure helpers
# ---------------------------------------------------------------------------

def test_extract_context_block_present():
    text = "<decomposer_context>\nkey: value\nother: stuff\n</decomposer_context>\n[{...}]"
    assert _extract_context_block(text) == "key: value\nother: stuff"


def test_extract_context_block_absent():
    assert _extract_context_block("[{...}]") == ""


def test_extract_context_block_strips_whitespace():
    text = "<decomposer_context>  \n  foo  \n  </decomposer_context>"
    assert _extract_context_block(text) == "foo"


def test_strip_context_block_removes_tag():
    text = "<decomposer_context>\nfoo\n</decomposer_context>\n[1, 2, 3]"
    result = _strip_context_block(text)
    assert "<decomposer_context>" not in result
    assert "[1, 2, 3]" in result


def test_strip_context_block_no_tag():
    text = "[1, 2, 3]"
    assert _strip_context_block(text) == "[1, 2, 3]"


def test_extract_json_array_plain():
    assert _extract_json_array('[{"id": "a"}]') == '[{"id": "a"}]'


def test_extract_json_array_fenced():
    text = "```json\n[{\"id\": \"a\"}]\n```"
    assert _extract_json_array(text) == '[{"id": "a"}]'


def test_extract_json_array_with_context_stripped():
    # After _strip_context_block the brackets from the context are gone.
    text = '[{"id": "st_1_a"}, {"id": "st_1_z"}]'
    result = _extract_json_array(text)
    assert '"st_1_a"' in result
    assert '"st_1_z"' in result


def test_context_block_with_brackets_does_not_confuse_array():
    """Brackets inside <decomposer_context> must not be mistaken for the JSON array."""
    raw = (
        "<decomposer_context>\n"
        "- src-layout: src/app/ [installed via hatchling]\n"
        "- Routes: [/v1/health, /v1/ingest]\n"
        "</decomposer_context>\n"
        '[{"id": "st_1_a", "agentId": "CodeAgent", "instruction": "x", "dependsOn": []}]'
    )
    ctx = _extract_context_block(raw)
    assert "src-layout" in ctx

    stripped = _strip_context_block(raw)
    arr = _extract_json_array(stripped)
    import json
    items = json.loads(arr)
    assert len(items) == 1
    assert items[0]["id"] == "st_1_a"


# ---------------------------------------------------------------------------
# Integration-level tests — decompose_subtask with mocked Agent
# ---------------------------------------------------------------------------

def _make_manifest(decomp_prompt: str = "decompose it") -> AgentManifest:
    return AgentManifest(
        agent_id="CodeAgent",
        domain="Engineering",
        tools=["file_read", "bash_exec_readonly"],
        mcp_servers=[],
        system_prompt="You are a coder.",
        decomposition_prompt=decomp_prompt,
    )


def _make_subtask(sid: str = "st_1", instruction: str = "Write 10 files") -> Subtask:
    return Subtask(id=sid, agent_id="CodeAgent", instruction=instruction, depends_on=[])


def _mock_agent_result(text: str) -> AgentResult:
    return AgentResult(
        task_id="task-test",
        agent_id="CodeAgent.decomposer",
        status=AgentStatus.success,
        output=AgentOutput(text=text, structured={}),
        input_tokens=100,
        output_tokens=50,
    )


# Agent is imported lazily inside decompose_subtask, so patch at the source module.
_AGENT_PATCH = "agentflow.agents.agent.Agent"


@pytest.mark.asyncio
async def test_decompose_returns_multi_subtasks_with_context():
    """Decomposer extracts context block and returns expanded micro-subtasks."""
    output = (
        "<decomposer_context>\n"
        "- src-layout under src/app/\n"
        "- SQLite with WAL mode\n"
        "</decomposer_context>\n"
        '[{"id": "st_1_a", "agentId": "CodeAgent", "instruction": "Write models", "dependsOn": []}, '
        '{"id": "st_1_b", "agentId": "CodeAgent", "instruction": "Write routes", "dependsOn": []}, '
        '{"id": "st_1_z", "agentId": "CodeAgent", "instruction": "Aggregate", "dependsOn": ["st_1_a", "st_1_b"]}]'
    )
    with patch(_AGENT_PATCH) as MockAgent:
        instance = MockAgent.return_value
        instance.run = AsyncMock(return_value=_mock_agent_result(output))

        subtasks, ctx = await decompose_subtask(
            _make_subtask(), _make_manifest(), "run-1", MagicMock(), MagicMock()
        )

    assert len(subtasks) == 3
    assert subtasks[0].id == "st_1_a"
    assert subtasks[2].depends_on == ["st_1_a", "st_1_b"]
    assert "src-layout" in ctx
    assert "SQLite" in ctx


@pytest.mark.asyncio
async def test_decompose_single_element_returns_original_with_context():
    """Single-element JSON array returns the original subtask but still extracts context."""
    output = (
        "<decomposer_context>\n"
        "Small task — only 2 files needed\n"
        "</decomposer_context>\n"
        '[{"id": "st_1", "agentId": "CodeAgent", "instruction": "Write 2 files", "dependsOn": []}]'
    )
    original = _make_subtask(instruction="Write 2 files")
    with patch(_AGENT_PATCH) as MockAgent:
        instance = MockAgent.return_value
        instance.run = AsyncMock(return_value=_mock_agent_result(output))

        subtasks, ctx = await decompose_subtask(
            original, _make_manifest(), "run-1", MagicMock(), MagicMock()
        )

    assert subtasks == [original]
    assert "Small task" in ctx


@pytest.mark.asyncio
async def test_decompose_no_context_block_returns_empty_string():
    """When the decomposer omits <decomposer_context>, context is empty string."""
    output = (
        '[{"id": "st_1_a", "agentId": "CodeAgent", "instruction": "x", "dependsOn": []}, '
        '{"id": "st_1_z", "agentId": "CodeAgent", "instruction": "agg", "dependsOn": ["st_1_a"]}]'
    )
    with patch(_AGENT_PATCH) as MockAgent:
        instance = MockAgent.return_value
        instance.run = AsyncMock(return_value=_mock_agent_result(output))

        subtasks, ctx = await decompose_subtask(
            _make_subtask(), _make_manifest(), "run-1", MagicMock(), MagicMock()
        )

    assert len(subtasks) == 2
    assert ctx == ""


@pytest.mark.asyncio
async def test_decompose_failed_agent_returns_original_empty_context():
    """If the decomposer agent fails, return ([original], '')."""
    failed = AgentResult(
        task_id="task-test",
        agent_id="CodeAgent.decomposer",
        status=AgentStatus.failed,
        output=AgentOutput(text="", structured={}),
        input_tokens=0,
        output_tokens=0,
    )
    original = _make_subtask()
    with patch(_AGENT_PATCH) as MockAgent:
        instance = MockAgent.return_value
        instance.run = AsyncMock(return_value=failed)

        subtasks, ctx = await decompose_subtask(
            original, _make_manifest(), "run-1", MagicMock(), MagicMock()
        )

    assert subtasks == [original]
    assert ctx == ""


@pytest.mark.asyncio
async def test_decompose_passes_task_and_user_context():
    """Top-level task string and user_context are forwarded to the decomposer agent."""
    with patch(_AGENT_PATCH) as MockAgent:
        instance = MockAgent.return_value
        instance.run = AsyncMock(return_value=_mock_agent_result(
            '[{"id":"st_1_a","agentId":"CodeAgent","instruction":"x","dependsOn":[]}, '
            '{"id":"st_1_z","agentId":"CodeAgent","instruction":"agg","dependsOn":["st_1_a"]}]'
        ))

        await decompose_subtask(
            _make_subtask(), _make_manifest(), "run-1", MagicMock(), MagicMock(),
            task="Top-level task description",
            user_context={"key": "val"},
        )

    envelope = instance.run.call_args[0][0]
    assert "Top-level task description" in envelope.instruction
    assert envelope.context.user_context == {"key": "val"}
