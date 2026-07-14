"""Built-in tool implementations registered into the global tool_registry."""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import time
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

async def _record_artifact(path: str) -> None:
    """Register a workspace-relative path with the current run's artifact sink."""
    from agentflow.tools.artifact_tracker import _current_sink
    sink = _current_sink.get()
    if sink is not None:
        await sink.record(path)


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

_ARXIV_ABS_RE = re.compile(r"arxiv\.org/abs/([\w./]+)", re.IGNORECASE)

async def _fetch_url(url: str) -> str:
    # Redirect arxiv abstract page URLs to the Atom API so we get structured
    # text (title + abstract) instead of raw JavaScript-heavy HTML.
    if m := _ARXIV_ABS_RE.search(url):
        paper_id = m.group(1).rstrip("/")
        api_url = f"https://export.arxiv.org/api/query?id_list={paper_id}&max_results=1"
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            try:
                resp = await client.get(api_url, headers=_HTTP_HEADERS)
                resp.raise_for_status()
                import xml.etree.ElementTree as ET
                root = ET.fromstring(resp.text)
                ns = "http://www.w3.org/2005/Atom"
                entry = root.find(f"{{{ns}}}entry")
                if entry is not None:
                    title = (entry.findtext(f"{{{ns}}}title") or "").strip()
                    summary = " ".join((entry.findtext(f"{{{ns}}}summary") or "").split())
                    return _truncate(f"**{title}**\n\n{summary}", label=url)
            except Exception:
                pass  # fall through to raw fetch on any error

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
    description=(
        "Fetch the raw text content of any URL (HTML, JSON, plain text). Returns up to 8 000 characters. "
        "arxiv.org/abs/ URLs are automatically resolved to title + abstract via the Atom API."
    ),
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
# web_search  (Tavily if TAVILY_API_KEY is set, else DuckDuckGo HTML fallback)
# ---------------------------------------------------------------------------

async def _web_search_tavily(query: str, max_results: int) -> str:
    payload = {"api_key": settings.tavily_api_key, "query": query, "max_results": max_results}
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.post("https://api.tavily.com/search", json=payload)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
        except Exception as exc:
            return f"Search error: {exc}"

    lines: list[str] = []
    if answer := data.get("answer"):
        lines.append(f"Summary: {answer}\n")
    for r in data.get("results", []):
        lines.append(f"• {r.get('title', '(no title)')}")
        lines.append(f"  {r.get('url', '')}")
        if content := r.get("content", ""):
            lines.append(f"  {content[:400]}")
        lines.append("")
    return "\n".join(lines) if lines else f"No results for: {query!r}"


async def _web_search_ddg(query: str, max_results: int) -> str:
    headers = {**_HTTP_HEADERS, "Accept": "text/html,application/xhtml+xml"}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        try:
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers=headers,
            )
            html = resp.text
        except Exception as exc:
            return f"Search error: {exc}"

    titles = re.findall(r'class="result__a"[^>]*>(.+?)</a>', html)
    urls = re.findall(r'class="result__url"[^>]*>\s*(.+?)\s*</', html)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.+?)</a>', html, re.DOTALL)

    lines: list[str] = []
    for title, url, snippet in list(zip(titles, urls, snippets))[:max_results]:
        lines.append(f"• {re.sub(r'<[^>]+>', '', title).strip()}")
        lines.append(f"  {url.strip()}")
        if clean := re.sub(r'<[^>]+>', '', snippet).strip():
            lines.append(f"  {clean[:400]}")
        lines.append("")
    return "\n".join(lines) if lines else f"No results for: {query!r}"


async def _web_search(query: str, max_results: int = 5) -> str:
    if settings.tavily_api_key:
        return await _web_search_tavily(query, max_results)
    return await _web_search_ddg(query, max_results)


tool_registry.register(ToolDefinition(
    name="web_search",
    description=(
        "Search the web for information on any topic. Returns titles, URLs, and content snippets. "
        "Uses Tavily when TAVILY_API_KEY is configured; falls back to DuckDuckGo HTML search otherwise."
    ),
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
            await _record_artifact(path)
            line_count = len(content.splitlines())
            return f"Wrote {line_count} lines ({len(content)} chars) to {path}"

        if mode == "append":
            with target.open("a", encoding="utf-8") as f:
                f.write(content)
            await _record_artifact(path)
            total_lines = len(target.read_text(encoding="utf-8").splitlines())
            return f"Appended {len(content)} chars to {path} (total: {total_lines} lines)"

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
            await _record_artifact(path)
            return f"Inserted {len(content)} chars before line {line} in {path}"

        if mode == "replace_lines":
            if start_line is None or end_line is None:
                return "Error: 'start_line' and 'end_line' required for replace_lines mode"
            lo = max(0, start_line - 1)
            hi = min(total, end_line)
            block = (content if content.endswith("\n") else content + "\n") if content else ""
            lines[lo:hi] = [block] if block else []
            new_text = "".join(lines)
            target.write_text(new_text, encoding="utf-8")
            await _record_artifact(path)
            new_total = len(new_text.splitlines())
            new_line_count = len(block.splitlines()) if block else 0
            new_end = lo + new_line_count
            preview = content[:3000] + ("…" if len(content) > 3000 else "")
            return (
                f"Replaced lines {start_line}-{end_line} in {path} "
                f"(file now {new_total} lines; new content at lines {lo + 1}-{new_end}):\n"
                f"{preview}"
            )

        if mode == "replace_pattern":
            if pattern is None:
                return "Error: 'pattern' parameter required for replace_pattern mode"
            new_text, count = re.subn(pattern, content, existing)
            if count == 0:
                return f"No matches for pattern {pattern!r} in {path}"
            target.write_text(new_text, encoding="utf-8")
            await _record_artifact(path)
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
            new_text = "".join(lines)
            target.write_text(new_text, encoding="utf-8")
            await _record_artifact(path)
            new_total = len(new_text.splitlines())
            new_line_count = len(block.splitlines()) if block else 0
            new_end = start_idx + 1 + new_line_count
            preview = content[:3000] + ("…" if len(content) > 3000 else "")
            return (
                f"Replaced content between lines {start_idx + 1} and {end_idx + 1} in {path} "
                f"(file now {new_total} lines; new content at lines {start_idx + 2}-{new_end}):\n"
                f"{preview}"
            )

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
        "mode='overwrite' (default): REPLACES THE ENTIRE FILE — use only for the first write or a deliberate full rewrite. "
        "mode='append': add content at end of file — preferred for adding sections to an existing file. "
        "mode='insert_at_line': insert content before the given line number (requires line). "
        "mode='replace_lines': replace a range of lines (requires start_line and end_line). "
        "mode='replace_pattern': find-and-replace using a regex (requires pattern; replaces all occurrences). "
        "mode='replace_between': replace text between two regex markers, keeping the marker lines (requires start_pattern and end_pattern). "
        "IMPORTANT: Do not call overwrite on the same file multiple times in one task — use append or a targeted mode to add or update content."
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
# bash_exec_readonly — safe subset of bash_exec for planning / decomposition
# ---------------------------------------------------------------------------

# Commands that are safe to run during read-only exploration.  Deliberately
# conservative: no interpreters (python3, node), no network tools (curl, wget),
# no editors, no process managers.
_READONLY_COMMANDS = frozenset({
    "find", "grep", "egrep", "fgrep",
    "ls", "cat", "head", "tail", "wc",
    "sort", "uniq", "diff", "comm",
    "echo", "printf", "test",
    "awk", "sed", "cut", "tr",
    "tree", "du", "stat", "file",
    "which", "basename", "dirname", "realpath", "pwd",
    "env", "printenv",
    "jq", "xargs",
})


def _check_readonly_command(command: str) -> str | None:
    """Return an error message if *command* is not safe for read-only use, else None."""
    # Block any output redirection
    if re.search(r'(?<![<&2])>{1,2}', command):
        return "Output redirections (> and >>) are not allowed in bash_exec_readonly"
    # Block sed in-place edits
    if re.search(r'\bsed\b[^|;]*-[a-zA-Z]*i', command):
        return "sed -i (in-place edit) is not allowed in bash_exec_readonly"
    # Split on shell operators to get individual pipeline stages
    segments = re.split(r'[|;&]+', command)
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        first_word = seg.split()[0] if seg.split() else ""
        cmd = os.path.basename(first_word)
        if cmd and cmd not in _READONLY_COMMANDS:
            allowed = ", ".join(sorted(_READONLY_COMMANDS))
            return f"Command '{cmd}' is not allowed in bash_exec_readonly. Allowed: {allowed}"
    return None


async def _bash_exec_readonly(command: str, purpose: str, timeout_seconds: int = 30) -> str:
    err = _check_readonly_command(command)
    if err:
        return f"Error: {err}"
    return await _bash_exec(command, purpose, timeout_seconds)


tool_registry.register(ToolDefinition(
    name="bash_exec_readonly",
    description=(
        "Execute a read-only bash command in the workspace. "
        "Suitable for workspace exploration: find, grep, ls, cat, wc, diff, etc. "
        "Write operations, output redirections, and arbitrary interpreters are blocked. "
        "Use only relative paths — '~' and absolute paths are not permitted."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Read-only shell command to run"},
            "purpose": {"type": "string", "description": "Short explanation of why this command is being run"},
            "timeout_seconds": {"type": "integer", "description": "Timeout in seconds (default 30)", "default": 30},
        },
        "required": ["command", "purpose"],
    },
    handler=_bash_exec_readonly,
    impact=ToolImpact.read_only,
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


async def _arxiv_search_handler(query: str, max_results: int = 5, category: str | None = None) -> str:
    from agentflow.tools.arxiv_search import arxiv_search as _arxiv_search

    try:
        papers = await asyncio.to_thread(_arxiv_search, query, max_results, category)
    except (ValueError, RuntimeError) as exc:
        return f"arXiv search error: {exc}"
    if not papers:
        return "No results found."
    lines: list[str] = []
    for p in papers:
        lines.append(f"**{p['title']}**")
        lines.append(f"URL: {p['url']}")
        if p.get("pdf_url"):
            lines.append(f"PDF: {p['pdf_url']}")
        if p["abstract"]:
            lines.append(f"Abstract: {p['abstract'][:600]}")
        lines.append("")
    return "\n".join(lines)


tool_registry.register(ToolDefinition(
    name="arxiv_search",
    description=(
        "Search academic papers on arXiv. Returns title, abstract, abstract URL, and PDF URL for each result. "
        "The abstract already contains the full paper summary — do NOT call fetch_url on arxiv links afterwards. "
        "Use download_document(pdf_url) to fetch the full PDF and ingest it into the knowledgebase. "
        "Use the category parameter to restrict results to a subject area and avoid off-topic hits "
        "(e.g. category='cs.LG' for ML, 'q-fin.TR' for trading, 'stat.ML' for statistical ML)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Free-text search query",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return (default 5)",
                "default": 5,
            },
            "category": {
                "type": "string",
                "description": (
                    "Optional arXiv subject category filter to reduce off-topic results. "
                    "Examples: 'cs.LG' (machine learning), 'cs.AI', 'q-fin.TR' (trading), "
                    "'q-fin.PM' (portfolio management), 'stat.ML', 'econ.EM'."
                ),
            },
        },
        "required": ["query"],
    },
    handler=_arxiv_search_handler,
    impact=ToolImpact.read_only,
))

# ---------------------------------------------------------------------------
# download_document  — fetch PDF / text / markdown to .downloads/ and ingest
# ---------------------------------------------------------------------------

_DOWNLOADS_DIR = ".downloads"

_MIME_TO_EXT: dict[str, str] = {
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/html": ".html",
}


async def _download_document(url: str, filename: str | None = None) -> str:
    from agentflow.tools.kb_dispatcher import _kb_dispatch_fn

    ws = _workspace()
    dl_dir = ws / _DOWNLOADS_DIR
    dl_dir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        try:
            resp = await client.get(url, headers=_HTTP_HEADERS)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return f"HTTP {exc.response.status_code} fetching {url}"
        except httpx.RequestError as exc:
            return f"Request error fetching {url}: {exc}"

    content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    ext = _MIME_TO_EXT.get(content_type)
    if ext is None:
        url_path = url.split("?")[0].rstrip("/")
        guessed, _ = mimetypes.guess_type(url_path)
        ext = _MIME_TO_EXT.get(guessed or "")
        if not ext:
            supported = ", ".join(sorted(_MIME_TO_EXT))
            return (
                f"Unsupported content type {content_type!r} from {url}. "
                f"Supported: {supported}"
            )

    if filename is None:
        url_stem = url.split("?")[0].rstrip("/").split("/")[-1] or "document"
        stem = url_stem.rsplit(".", 1)[0] if "." in url_stem else url_stem
        filename = f"{stem}{ext}"

    saved = dl_dir / filename
    if saved.exists():
        stem2, suf = saved.stem, saved.suffix
        saved = dl_dir / f"{stem2}_{int(time.time())}{suf}"

    saved.write_bytes(resp.content)
    rel_path = str(saved.relative_to(ws))
    await _record_artifact(rel_path)

    msg = f"Downloaded {len(resp.content):,} bytes → {rel_path}"

    dispatch = _kb_dispatch_fn.get()
    if dispatch is not None:
        try:
            kb_result = await dispatch(
                f"Ingest the downloaded document at workspace path '{rel_path}' into the knowledgebase."
            )
            msg += f"\nKB ingest: {kb_result}"
        except Exception as exc:
            logger.warning("KB ingest dispatch failed for %s: %s", rel_path, exc)
            msg += f"\nKB ingest skipped (no KnowledgebaseAgent available): {exc}"
    else:
        msg += f"\n(KB ingest skipped — KnowledgebaseAgent not active in this run)"

    return msg


tool_registry.register(ToolDefinition(
    name="download_document",
    description=(
        "Download a document (PDF, plain text, or Markdown) from a URL and save it to the "
        "'.downloads/' folder in the workspace. Supported content types: PDF, text/plain, "
        "text/markdown. If KnowledgebaseAgent is part of this run the saved file is automatically "
        "ingested into the semantic knowledgebase. Use the pdf_url returned by arxiv_search to "
        "fetch and ingest full papers."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL of the document to download"},
            "filename": {
                "type": "string",
                "description": "Optional filename to save as (inside .downloads/). Derived from URL if omitted.",
            },
        },
        "required": ["url"],
    },
    handler=_download_document,
    impact=ToolImpact.write,
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
