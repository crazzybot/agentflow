"""Tests for the tool registry and built-in tool implementations."""
import pytest
import agentflow.tools  # noqa: F401 — ensures built-ins are registered

from agentflow.tools.builtin import write_overflow_file
from agentflow.tools.registry import tool_registry


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
    stub_names = ["sql_query", "lint", "spell_check"]
    for name in stub_names:
        assert tool_registry.get(name) is not None, f"Stub {name!r} not registered"


def test_arxiv_search_registered():
    assert tool_registry.get("arxiv_search") is not None


def test_download_document_registered():
    tool = tool_registry.get("download_document")
    assert tool is not None
    assert "url" in tool.input_schema["properties"]


# ---------------------------------------------------------------------------
# Tool execution tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_file_write_and_read(tmp_path, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path))

    result = await tool_registry.execute("file_write", {"path": "test.txt", "content": "hello world"})
    assert "Wrote" in result

    result = await tool_registry.execute("file_read", {"path": "test.txt"})
    assert "hello world" in result


@pytest.mark.asyncio
async def test_file_write_returns_line_count(tmp_path, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path))

    content = "line one\nline two\nline three"
    result = await tool_registry.execute("file_write", {"path": "lines.txt", "content": content})
    assert "3 lines" in result
    assert "Wrote" in result


@pytest.mark.asyncio
async def test_file_write_append_returns_total_line_count(tmp_path, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path))

    await tool_registry.execute("file_write", {"path": "app.txt", "content": "line1\nline2\n"})
    result = await tool_registry.execute(
        "file_write", {"path": "app.txt", "content": "line3\n", "mode": "append"}
    )
    assert "total: 3 lines" in result


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
async def test_unknown_tool_returns_error():
    result = await tool_registry.execute("totally_made_up_tool", {"x": 1})
    assert "Unknown tool" in result


@pytest.mark.asyncio
async def test_path_traversal_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path))
    result = await tool_registry.execute("file_read", {"path": "../../../etc/passwd"})
    assert "traversal" in result.lower() or "not allowed" in result.lower()


# ---------------------------------------------------------------------------
# Result-size budget: max_result_chars / write_overflow_file (Fix 1)
# ---------------------------------------------------------------------------

def test_most_tools_default_to_a_result_char_budget():
    for name in ("fetch_url", "bash_exec", "python_exec", "web_search", "wikipedia"):
        tool = tool_registry.get(name)
        assert tool is not None
        assert tool.max_result_chars == 8_000


def test_file_read_is_exempt_from_the_generic_result_cap():
    """file_read manages its own budget via max_lines/file_read_max_chars with
    structured from_line/to_line pointers — a second, uncoordinated cap on top
    would desync the header's claimed to_line from what actually gets returned."""
    tool = tool_registry.get("file_read")
    assert tool is not None
    assert tool.max_result_chars is None


def test_write_overflow_file_spills_to_disk_and_returns_pointer(tmp_path, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path))

    full_text = "A" * 5_000 + "B" * 5_000  # 10k chars, well over the preview window
    message = write_overflow_file("bash_exec", "toolu_01ABC", full_text)

    assert "10,000 chars" in message
    assert ".tool_output/bash_exec_toolu_01ABC.txt" in message
    assert "use file_read" in message.lower()
    # Head and tail are both represented in the preview (not just the head —
    # errors/final results in e.g. bash stdout often land at the end).
    assert "A" * 100 in message
    assert "B" * 100 in message

    spilled = tmp_path / ".tool_output" / "bash_exec_toolu_01ABC.txt"
    assert spilled.exists()
    assert spilled.read_text() == full_text


def test_write_overflow_file_sanitizes_call_id_for_filesystem_safety(tmp_path, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path))

    write_overflow_file("bash_exec", "toolu/../../etc", "x" * 20_000)

    out_dir = tmp_path / ".tool_output"
    assert out_dir.exists()
    for f in out_dir.iterdir():
        assert f.is_relative_to(out_dir)


def test_write_overflow_file_does_not_truncate_small_results(tmp_path, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path))

    message = write_overflow_file("bash_exec", "toolu_small", "short output")
    assert message.endswith("short output")
    assert "omitted" not in message


# ---------------------------------------------------------------------------
# file_read char-budget cap
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_file_read_char_budget_caps_pathologically_long_lines(tmp_path, monkeypatch):
    """A file with very few, very long lines can blow past a reasonable response
    size even while well under max_lines — the char budget must catch it and
    still report an honest, forward-progressing from_line/to_line pointer."""
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path))
    monkeypatch.setattr("agentflow.config.settings.file_read_max_chars", 1_000)

    long_line = "x" * 2_000
    (tmp_path / "long.txt").write_text("\n".join([long_line] * 5))

    result = await tool_registry.execute("file_read", {"path": "long.txt"})

    assert "from_line=1" in result
    assert "total_lines=5" in result
    assert "use start_line=" in result  # more content remains
    # At least one full line always makes it through, even over budget, so a
    # follow-up call makes forward progress instead of looping on an empty read.
    assert "x" * 2_000 in result


@pytest.mark.asyncio
async def test_file_read_pattern_match_reports_truncation_honestly(tmp_path, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path))
    monkeypatch.setattr("agentflow.config.settings.file_read_max_chars", 500)

    lines = [f"needle {i} " + "pad " * 50 for i in range(20)]
    (tmp_path / "matches.txt").write_text("\n".join(lines))

    result = await tool_registry.execute(
        "file_read", {"path": "matches.txt", "pattern": "needle", "context_lines": 0}
    )

    assert "matches=20" in result
    assert "of 20 matches" in result
    assert "narrow the pattern" in result


# ---------------------------------------------------------------------------
# file_write preview wording (never implies an incomplete write)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_file_write_replace_lines_preview_labeled_not_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path))

    await tool_registry.execute(
        "file_write", {"path": "doc.txt", "content": "line1\nline2\nline3\n"}
    )
    big_content = "y" * 5_000
    result = await tool_registry.execute(
        "file_write",
        {"path": "doc.txt", "content": big_content, "mode": "replace_lines", "start_line": 2, "end_line": 2},
    )

    assert "written in full" in result
    assert "…" in result  # preview itself is still shortened for the response...
    # ...but the file on disk has the complete content, not the 300-char preview.
    assert (tmp_path / "doc.txt").read_text().count("y") == 5_000
