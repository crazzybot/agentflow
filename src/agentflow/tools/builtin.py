"""Built-in tool implementations registered into the global tool_registry."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

from agentflow.config import settings
from agentflow.tools.registry import ToolDefinition, ToolImpact, tool_registry

logger = logging.getLogger(__name__)

_HTTP_HEADERS = {"User-Agent": "AgentFlow/0.1 (https://github.com/agentflow)"}
_MAX_CONTENT = 8_000  # chars returned to the LLM


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _workspace() -> Path:
    p = Path(settings.workspace_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


def _safe_path(relative: str) -> Path | None:
    """Resolve *relative* inside the workspace; return None on traversal."""
    ws = _workspace()
    target = (ws / relative).resolve()
    if not str(target).startswith(str(ws)):
        return None
    return target


def _truncate(text: str, label: str = "") -> str:
    if len(text) <= _MAX_CONTENT:
        return text
    omitted = len(text) - _MAX_CONTENT
    suffix = f"\n… [{omitted} chars truncated{(' in ' + label) if label else ''}]"
    return text[:_MAX_CONTENT] + suffix


# ---------------------------------------------------------------------------
# fetch_url
# ---------------------------------------------------------------------------

async def _fetch_url(url: str) -> str:
    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
        try:
            resp = await client.get(url, headers=_HTTP_HEADERS)
            resp.raise_for_status()
            return _truncate(resp.text, label=url)
        except httpx.HTTPStatusError as exc:
            return f"HTTP {exc.response.status_code} for {url}"
        except httpx.RequestError as exc:
            return f"Request error: {exc}"


tool_registry.register(ToolDefinition(
    name="fetch_url",
    description="Fetch the raw text content of any URL (HTML, JSON, plain text). Returns up to 8 000 characters.",
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch"},
        },
        "required": ["url"],
    },
    handler=_fetch_url,
    impact=ToolImpact.read_only,
))


# ---------------------------------------------------------------------------
# web_search  (DuckDuckGo Instant Answers — no API key required)
# ---------------------------------------------------------------------------

async def _web_search(query: str, max_results: int = 5) -> str:
    params = {"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"}
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get("https://api.duckduckgo.com/", params=params, headers=_HTTP_HEADERS)
            data: dict[str, Any] = resp.json()
        except Exception as exc:
            return f"Search error: {exc}"

    lines: list[str] = []
    if data.get("Abstract"):
        lines.append(f"Abstract: {data['Abstract']}")
        if data.get("AbstractURL"):
            lines.append(f"Source: {data['AbstractURL']}")
    for item in data.get("RelatedTopics", [])[:max_results]:
        if not isinstance(item, dict):
            continue
        # RelatedTopics can nest topic groups
        topics = item.get("Topics") or [item]
        for t in topics[:2]:
            if t.get("Text"):
                lines.append(f"• {t['Text']}")
                if t.get("FirstURL"):
                    lines.append(f"  {t['FirstURL']}")

    return "\n".join(lines) if lines else f"No results for: {query!r}"


tool_registry.register(ToolDefinition(
    name="web_search",
    description="Search the web using DuckDuckGo instant answers. Returns abstracts and related topics.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query"},
            "max_results": {"type": "integer", "description": "Maximum number of results to return", "default": 5},
        },
        "required": ["query"],
    },
    handler=_web_search,
    impact=ToolImpact.read_only,
))


# ---------------------------------------------------------------------------
# wikipedia
# ---------------------------------------------------------------------------

async def _wikipedia(topic: str) -> str:
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{topic.replace(' ', '_')}"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(url, headers=_HTTP_HEADERS)
            if resp.status_code == 404:
                return f"No Wikipedia article found for: {topic!r}"
            data = resp.json()
            return f"{data.get('title', topic)}\n\n{data.get('extract', 'No extract available.')}"
        except Exception as exc:
            return f"Wikipedia error: {exc}"


tool_registry.register(ToolDefinition(
    name="wikipedia",
    description="Fetch a plain-English summary of a Wikipedia article by topic name.",
    input_schema={
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "Wikipedia article title or topic"},
        },
        "required": ["topic"],
    },
    handler=_wikipedia,
    impact=ToolImpact.read_only,
))


# ---------------------------------------------------------------------------
# file_read / file_write  (workspace-sandboxed)
# ---------------------------------------------------------------------------

def _numbered(lines: list[str], offset: int) -> str:
    """Format lines with 1-indexed line numbers starting at offset+1."""
    return "".join(f"{offset + i + 1:4}: {line}" for i, line in enumerate(lines))


async def _file_read(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    pattern: str | None = None,
    context_lines: int = 5,
    max_lines: int | None = None,
    include_line_numbers: bool = True,
) -> str:
    target = _safe_path(path)
    if target is None:
        return "Error: path traversal outside workspace is not allowed"
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"File not found: {path}"
    except Exception as exc:
        return f"Read error: {exc}"

    lines = text.splitlines(keepends=True)
    total_lines = len(lines)
    limit = max_lines if max_lines is not None else settings.file_read_max_lines

    if pattern is not None:
        blocks: list[str] = []
        seen: set[int] = set()
        for i, line in enumerate(lines):
            if re.search(pattern, line):
                lo = max(0, i - context_lines)
                hi = min(total_lines, i + context_lines + 1)
                new_indices = [j for j in range(lo, hi) if j not in seen]
                if not new_indices:
                    continue
                seen.update(new_indices)
                block_lines = lines[lo:hi]
                rendered = _numbered(block_lines, lo) if include_line_numbers else "".join(block_lines)
                blocks.append(f"--- match at line {i + 1} ---\n{rendered}")
        if not blocks:
            return f"[total_lines={total_lines}]\nNo matches for pattern {pattern!r} in {path}"
        return f"[total_lines={total_lines}]\n" + "\n".join(blocks)

    lo = max(0, (start_line - 1) if start_line is not None else 0)
    hi = min(total_lines, end_line if end_line is not None else total_lines)

    # Apply max_lines cap
    if hi - lo > limit:
        hi = lo + limit

    selected = lines[lo:hi]
    from_line = lo + 1
    to_line = lo + len(selected)
    has_more = to_line < total_lines

    meta = f"[from_line={from_line}, to_line={to_line}, total_lines={total_lines}]"
    if has_more:
        meta += f"  ← use start_line={to_line + 1} to read more"

    content = _numbered(selected, lo) if include_line_numbers else "".join(selected)
    return meta + "\n" + content


async def _file_write(
    path: str,
    content: str,
    mode: str = "overwrite",
    line: int | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    pattern: str | None = None,
    start_pattern: str | None = None,
    end_pattern: str | None = None,
) -> str:
    target = _safe_path(path)
    if target is None:
        return "Error: path traversal outside workspace is not allowed"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)

        if mode == "overwrite":
            target.write_text(content, encoding="utf-8")
            return f"Wrote {len(content)} chars to {path}"

        if mode == "append":
            with target.open("a", encoding="utf-8") as f:
                f.write(content)
            return f"Appended {len(content)} chars to {path}"

        # Remaining modes require an existing file
        try:
            existing = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return f"File not found: {path} (mode={mode!r} requires an existing file)"

        lines = existing.splitlines(keepends=True)
        total = len(lines)

        if mode == "insert_at_line":
            if line is None:
                return "Error: 'line' parameter required for insert_at_line mode"
            block = content if content.endswith("\n") else content + "\n"
            idx = max(0, min(line - 1, total))
            lines.insert(idx, block)
            target.write_text("".join(lines), encoding="utf-8")
            return f"Inserted {len(content)} chars before line {line} in {path}"

        if mode == "replace_lines":
            if start_line is None or end_line is None:
                return "Error: 'start_line' and 'end_line' required for replace_lines mode"
            lo = max(0, start_line - 1)
            hi = min(total, end_line)
            block = (content if content.endswith("\n") else content + "\n") if content else ""
            lines[lo:hi] = [block] if block else []
            target.write_text("".join(lines), encoding="utf-8")
            return f"Replaced lines {start_line}-{end_line} in {path}"

        if mode == "replace_pattern":
            if pattern is None:
                return "Error: 'pattern' parameter required for replace_pattern mode"
            new_text, count = re.subn(pattern, content, existing)
            if count == 0:
                return f"No matches for pattern {pattern!r} in {path}"
            target.write_text(new_text, encoding="utf-8")
            return f"Replaced {count} occurrence(s) of {pattern!r} in {path}"

        if mode == "replace_between":
            if start_pattern is None or end_pattern is None:
                return "Error: 'start_pattern' and 'end_pattern' required for replace_between mode"
            start_idx = end_idx = None
            for i, ln in enumerate(lines):
                if start_idx is None and re.search(start_pattern, ln):
                    start_idx = i
                elif start_idx is not None and re.search(end_pattern, ln):
                    end_idx = i
                    break
            if start_idx is None:
                return f"Start pattern {start_pattern!r} not found in {path}"
            if end_idx is None:
                return f"End pattern {end_pattern!r} not found after line {start_idx + 1} in {path}"
            block = (content if content.endswith("\n") else content + "\n") if content else ""
            lines[start_idx + 1:end_idx] = [block] if block else []
            target.write_text("".join(lines), encoding="utf-8")
            return f"Replaced content between lines {start_idx + 1} and {end_idx + 1} in {path}"

        return f"Error: unknown mode {mode!r}"

    except Exception as exc:
        return f"Write error: {exc}"


tool_registry.register(ToolDefinition(
    name="file_read",
    description=(
        "Read a file from the agent workspace. Path is relative to the workspace root. "
        "Always returns a metadata header '[from_line=X, to_line=Y, total_lines=Z]' followed "
        "by the file content with line numbers (use include_line_numbers=false to omit them). "
        "At most max_lines lines are returned per call (default from settings, typically 200); "
        "if the file is larger the header says 'use start_line=N to read more'. "
        "Use start_line/end_line to read a specific range (1-indexed, inclusive). "
        "Use pattern (regex) to return matching lines plus context_lines of surrounding context. "
        "Prefer this tool over bash cat/head/tail for reading files."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative file path"},
            "start_line": {"type": "integer", "description": "First line to read, 1-indexed inclusive (omit for beginning of file)"},
            "end_line": {"type": "integer", "description": "Last line to read, 1-indexed inclusive (omit for end of file)"},
            "pattern": {"type": "string", "description": "Regex to search for; returns each matching line with surrounding context"},
            "context_lines": {"type": "integer", "description": "Lines of context before/after each pattern match (default 5)", "default": 5},
            "max_lines": {"type": "integer", "description": "Maximum lines to return in this call (overrides the default limit)"},
            "include_line_numbers": {"type": "boolean", "description": "Prefix each line with its line number (default true)", "default": True},
        },
        "required": ["path"],
    },
    handler=_file_read,
    impact=ToolImpact.read_only,
))

tool_registry.register(ToolDefinition(
    name="file_write",
    description=(
        "Write content to a file in the agent workspace. Creates parent directories automatically. "
        "mode='overwrite' (default): replace entire file. "
        "mode='append': add content at end of file. "
        "mode='insert_at_line': insert content before the given line number (requires line). "
        "mode='replace_lines': replace a range of lines (requires start_line and end_line). "
        "mode='replace_pattern': find-and-replace using a regex (requires pattern; replaces all occurrences). "
        "mode='replace_between': replace text between two regex markers, keeping the marker lines (requires start_pattern and end_pattern)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative file path"},
            "content": {"type": "string", "description": "Content to write / insert / use as replacement"},
            "mode": {
                "type": "string",
                "enum": ["overwrite", "append", "insert_at_line", "replace_lines", "replace_pattern", "replace_between"],
                "description": "Write mode (default: overwrite)",
                "default": "overwrite",
            },
            "line": {"type": "integer", "description": "insert_at_line: insert before this 1-indexed line"},
            "start_line": {"type": "integer", "description": "replace_lines: first line to replace, 1-indexed inclusive"},
            "end_line": {"type": "integer", "description": "replace_lines: last line to replace, 1-indexed inclusive"},
            "pattern": {"type": "string", "description": "replace_pattern: regex pattern to find and replace"},
            "start_pattern": {"type": "string", "description": "replace_between: regex marking the start boundary line (kept)"},
            "end_pattern": {"type": "string", "description": "replace_between: regex marking the end boundary line (kept)"},
        },
        "required": ["path", "content"],
    },
    handler=_file_write,
    impact=ToolImpact.write,
))


# ---------------------------------------------------------------------------
# bash_exec
# ---------------------------------------------------------------------------

async def _bash_exec(command: str, purpose: str, timeout_seconds: int = 30) -> str:
    # Block ~ paths — they escape the workspace sandbox
    if re.search(r'(?:^|[\s;|&`(])~[/\s]|(?:^|[\s;|&`(])~$', command):
        return (
            "Error: '~' paths are not allowed. "
            "The workspace is already your current directory — use relative paths only."
        )

    workspace = _workspace()
    # Override HOME so any residual ~ expansion stays inside the workspace
    env = {**os.environ, "HOME": str(workspace)}

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(workspace),
            env=env,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        output = stdout.decode(errors="replace")
        return f"exit_code={proc.returncode}\n{_truncate(output, label='stdout')}"
    except asyncio.TimeoutError:
        return f"Command timed out after {timeout_seconds}s"
    except Exception as exc:
        return f"Exec error: {exc}"


tool_registry.register(ToolDefinition(
    name="bash_exec",
    description=(
        "Execute a bash command with the workspace as the current working directory. "
        "Use only relative paths — absolute paths and '~' are not permitted and will be rejected. "
        "Returns stdout+stderr and exit code."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run"},
            "purpose": {"type": "string", "description": "Short explanation of why this command is being run"},
            "timeout_seconds": {"type": "integer", "description": "Timeout in seconds (default 30)", "default": 30},
        },
        "required": ["command", "purpose"],
    },
    handler=_bash_exec,
    impact=ToolImpact.execute,
))


# ---------------------------------------------------------------------------
# python_exec
# ---------------------------------------------------------------------------

def _sandbox_python() -> str:
    """Return the Python interpreter for python_exec, falling back to python3."""
    configured = settings.sandbox_python.strip()
    if not configured:
        return "python3"
    p = Path(configured)
    if not p.is_absolute():
        p = Path.cwd() / p
    if p.exists():
        return str(p)
    logger.warning("sandbox_python %r not found, falling back to python3", configured)
    return "python3"


async def _python_exec(code: str, purpose: str, timeout_seconds: int = 30) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            _sandbox_python(), "-c", code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(_workspace()),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        output = stdout.decode(errors="replace")
        return f"exit_code={proc.returncode}\n{_truncate(output, label='python output')}"
    except asyncio.TimeoutError:
        return f"Python exec timed out after {timeout_seconds}s"
    except Exception as exc:
        return f"Python exec error: {exc}"


tool_registry.register(ToolDefinition(
    name="python_exec",
    description="Execute a Python code snippet in the workspace directory. Returns stdout+stderr and exit code.",
    input_schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python code to execute"},
            "purpose": {"type": "string", "description": "Short explanation of why this code is being run"},
            "timeout_seconds": {"type": "integer", "description": "Timeout in seconds (default 30)", "default": 30},
        },
        "required": ["code", "purpose"],
    },
    handler=_python_exec,
    impact=ToolImpact.execute,
))


# ---------------------------------------------------------------------------
# Stub tools — placeholders for tools that require external integrations
# ---------------------------------------------------------------------------

def _make_stub(tool_name: str, description: str, required_params: list[str], impact: ToolImpact) -> ToolDefinition:
    async def stub_handler(**kwargs: Any) -> str:
        return (
            f"Tool '{tool_name}' is not yet integrated. "
            f"To enable it, connect the appropriate MCP server or implement the handler in tools/builtin.py. "
            f"Received input: {json.dumps(kwargs, default=str)}"
        )

    props = {p: {"type": "string"} for p in required_params}
    return ToolDefinition(
        name=tool_name,
        description=description,
        input_schema={"type": "object", "properties": props, "required": required_params},
        handler=stub_handler,
        impact=impact,
    )


async def _arxiv_search_handler(query: str, max_results: int = 10) -> str:
    from agentflow.tools.arxiv_search import arxiv_search as _arxiv_search

    try:
        urls = await asyncio.to_thread(_arxiv_search, query, max_results)
    except (ValueError, RuntimeError) as exc:
        return f"arXiv search error: {exc}"
    return "\n".join(urls) if urls else "No results found."


tool_registry.register(ToolDefinition(
    name="arxiv_search",
    description="Search academic papers on arXiv and return a list of abstract URLs.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Free-text search query",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return (default 10)",
                "default": 10,
            },
        },
        "required": ["query"],
    },
    handler=_arxiv_search_handler,
    impact=ToolImpact.read_only,
))

_STUBS: list[tuple[str, str, list[str], ToolImpact]] = [
    ("sql_query",        "Execute a SQL query against a configured database",       ["query"],               ToolImpact.execute),
    ("chart_gen",        "Generate a chart from data and return a file path",       ["data", "chart_type"],  ToolImpact.write),
    ("csv_parse",        "Parse a CSV file and return structured data",             ["path"],                ToolImpact.read_only),
    ("spell_check",      "Check spelling and grammar in a text passage",            ["text"],                ToolImpact.read_only),
    ("readability_score","Compute readability metrics for a passage",               ["text"],                ToolImpact.read_only),
    ("tone_analyzer",    "Analyze the tone and sentiment of a passage",             ["text"],                ToolImpact.read_only),
    ("dependency_graph", "Build a dependency graph from a task list",               ["tasks"],               ToolImpact.read_only),
    ("timeline_gen",     "Generate a project timeline from phases",                 ["phases"],              ToolImpact.write),
    ("risk_model",       "Produce a risk matrix for a set of risks",                ["risks"],               ToolImpact.read_only),
    ("lint",             "Run a linter on source code",                             ["code", "language"],    ToolImpact.read_only),
    ("test_runner",      "Run a test suite and return results",                     ["test_path"],           ToolImpact.execute),
]

for _name, _desc, _params, _impact in _STUBS:
    tool_registry.register(_make_stub(_name, _desc, _params, _impact))
