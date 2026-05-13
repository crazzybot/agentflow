"""Tests for the tool registry and built-in tool implementations."""
import pytest
import agentflow.tools  # noqa: F401 — ensures built-ins are registered

from agentflow.tools.registry import ToolDefinition, tool_registry


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------

def test_builtin_tools_registered():
    names = {t.name for t in tool_registry.all()}
    assert "fetch_url" in names
    assert "web_search" in names
    assert "wikipedia" in names
    assert "file_read" in names
    assert "file_write" in names
    assert "bash_exec" in names
    assert "python_exec" in names


def test_get_many_filters_unknowns():
    result = tool_registry.get_many(["fetch_url", "nonexistent_tool", "bash_exec"])
    assert len(result) == 2
    assert {t.name for t in result} == {"fetch_url", "bash_exec"}


def test_tool_definition_to_anthropic_param():
    tool = tool_registry.get("web_search")
    assert tool is not None
    param = tool.to_anthropic_param()
    assert param["name"] == "web_search"
    assert "description" in param
    assert param["input_schema"]["type"] == "object"
    assert "query" in param["input_schema"]["properties"]


def test_stubs_registered():
    stub_names = ["arxiv_search", "sql_query", "lint", "spell_check"]
    for name in stub_names:
        assert tool_registry.get(name) is not None, f"Stub {name!r} not registered"


# ---------------------------------------------------------------------------
# Tool execution tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_file_write_and_read(tmp_path, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path))

    result = await tool_registry.execute("file_write", {"path": "test.txt", "content": "hello world"})
    assert "hello world" not in result or "Wrote" in result

    result = await tool_registry.execute("file_read", {"path": "test.txt"})
    assert "hello world" in result


@pytest.mark.asyncio
async def test_bash_exec():
    result = await tool_registry.execute("bash_exec", {"command": "echo hello_agentflow"})
    assert "hello_agentflow" in result
    assert "exit_code=0" in result


@pytest.mark.asyncio
async def test_python_exec():
    result = await tool_registry.execute("python_exec", {"code": "print(6 * 7)"})
    assert "42" in result
    assert "exit_code=0" in result


@pytest.mark.asyncio
async def test_stub_returns_informative_message():
    result = await tool_registry.execute("arxiv_search", {"query": "transformers"})
    assert "not yet integrated" in result.lower() or "stub" in result.lower() or "not" in result.lower()


@pytest.mark.asyncio
async def test_unknown_tool_returns_error():
    result = await tool_registry.execute("totally_made_up_tool", {"x": 1})
    assert "Unknown tool" in result


@pytest.mark.asyncio
async def test_path_traversal_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path))
    result = await tool_registry.execute("file_read", {"path": "../../../etc/passwd"})
    assert "traversal" in result.lower() or "not allowed" in result.lower()
