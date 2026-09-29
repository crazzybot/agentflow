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
    result = await tool_registry.execute("bash_exec", {"command": "echo hello_agentflow", "purpose": "test"})
    assert "hello_agentflow" in result
    assert "exit_code=0" in result


@pytest.mark.asyncio
async def test_python_exec():
    result = await tool_registry.execute("python_exec", {"code": "print(6 * 7)", "purpose": "test"})
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


# ---------------------------------------------------------------------------
# fetch_url HTML → text conversion
# ---------------------------------------------------------------------------

def test_html_to_text_strips_markup_and_chrome():
    from agentflow.tools.builtin import html_to_text

    page = """<!doctype html><html><head><title>Quarterly  Results</title>
    <style>.x{color:red}</style><script>var tracking = 1;</script></head>
    <body><nav><a href="/">Home</a> | <a href="/about">About</a></nav>
    <h1>Revenue up 12%</h1><p>Revenue rose to <b>$4.2bn</b> &amp; margins held.</p>
    <ul><li>EPS: $1.10</li><li>Guidance raised</li></ul>
    <table><tr><th>Q</th><th>Rev</th></tr><tr><td>Q3</td><td>4.2</td></tr></table>
    <footer>Copyright 2026</footer></body></html>"""

    text = html_to_text(page)

    assert text.startswith("# Quarterly Results")
    assert "# Revenue up 12%" in text
    assert "Revenue rose to $4.2bn & margins held." in text
    assert "- EPS: $1.10" in text
    assert "Q3" in text and "4.2" in text
    for junk in ("tracking", "color:red", "Home", "About", "Copyright", "<"):
        assert junk not in text


def test_looks_like_html_detection():
    from agentflow.tools.builtin import _looks_like_html

    assert _looks_like_html("text/html; charset=utf-8", "")
    assert _looks_like_html("", "  <!DOCTYPE html><html></html>")
    assert not _looks_like_html("application/json", '{"a": 1}')
    assert not _looks_like_html("text/plain", "plain text")


def test_html_to_text_prefers_main_content():
    from agentflow.tools.builtin import html_to_text

    page = "<body><div>Sidebar promo</div><main><h2>Story</h2><p>Body text.</p></main></body>"
    text = html_to_text(page)
    assert "Body text." in text
    assert "Sidebar promo" not in text


# ---------------------------------------------------------------------------
# Sandbox hardening: path containment, env scrubbing, read-only shell allowlist
# ---------------------------------------------------------------------------

from agentflow.tools.builtin import _check_readonly_command, _safe_path, _sandbox_env  # noqa: E402


def test_safe_path_rejects_sibling_dir_sharing_the_workspace_prefix(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    (tmp_path / "workspace-other").mkdir()
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(ws))
    assert _safe_path("../workspace-other/secret.txt") is None
    assert _safe_path("sub/ok.txt") == (ws / "sub/ok.txt").resolve()


def test_sandbox_env_withholds_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("REDIS_URL", "redis://secret")
    env = _sandbox_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "REDIS_URL" not in env
    assert "PATH" in env
    assert env["HOME"] == str(tmp_path.resolve())


def test_sandbox_env_passthrough_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path))
    monkeypatch.setattr("agentflow.config.settings.sandbox_env_passthrough", ["NODE_OPTIONS"])
    monkeypatch.setenv("NODE_OPTIONS", "--max-old-space-size=4096")
    assert _sandbox_env()["NODE_OPTIONS"] == "--max-old-space-size=4096"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,inp", [
    ("bash_exec", {"command": "echo key=$ANTHROPIC_API_KEY", "purpose": "t"}),
    ("python_exec", {"code": "import os; print('key=' + os.environ.get('ANTHROPIC_API_KEY', ''))", "purpose": "t"}),
])
async def test_exec_tools_cannot_read_api_key(tool, inp, tmp_path, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    result = await tool_registry.execute(tool, inp)
    assert "exit_code=0" in result
    assert "sk-secret" not in result


@pytest.mark.parametrize("command", [
    "find . -name '*.py'",
    "grep -rn 'def run' src | head -20",
    "ls -la && cat README.md",
    "find . -type f 2>/dev/null | wc -l",
    "grep -rn \"def \\w+(\" src 2>&1",
    "wc -l src/*.py",
    "jq '.subtasks[0]' plan.json",
    "sort -u names.txt | uniq -c",
])
def test_readonly_allows_exploration_commands(command, tmp_path, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path))
    assert _check_readonly_command(command) is None


@pytest.mark.parametrize("command", [
    "env",
    "printenv ANTHROPIC_API_KEY",
    "env rm -rf .",
    "find . | xargs rm",
    "awk 'BEGIN{system(\"id\")}'",
    "sed -n 1p x",
    "echo $(id)",
    "echo `id`",
    "echo \"$ANTHROPIC_API_KEY\"",
    "ls\nrm -rf .",
    "find . -exec rm {} \\;",
    "find . -exec rm '{}' ';'",
    "find . -delete",
    "sort -o out.txt in.txt",
    "uniq in.txt out.txt",
    "tree -o out.txt",
    "cat ../.env",
    "cat {..,x}/.env",
    "cat .[.]/.env",
    "cat .*/.env",
    "cat /etc/passwd",
    "grep --file=/etc/passwd x",
    "ls > listing.txt",
    "cat < ../.env",
    "(cd .. && cat .env)",
    "cat 'unterminated",
])
def test_readonly_rejects_escapes(command, tmp_path, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path))
    assert _check_readonly_command(command) is not None
