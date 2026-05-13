"""Built-in tool implementations registered into the global tool_registry."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from agentflow.config import settings
from agentflow.tools.registry import ToolDefinition, tool_registry

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
))


# ---------------------------------------------------------------------------
# file_read / file_write  (workspace-sandboxed)
# ---------------------------------------------------------------------------

async def _file_read(path: str) -> str:
    target = _safe_path(path)
    if target is None:
        return "Error: path traversal outside workspace is not allowed"
    try:
        return target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"File not found: {path}"
    except Exception as exc:
        return f"Read error: {exc}"


async def _file_write(path: str, content: str) -> str:
    target = _safe_path(path)
    if target is None:
        return "Error: path traversal outside workspace is not allowed"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} chars to {path}"
    except Exception as exc:
        return f"Write error: {exc}"


tool_registry.register(ToolDefinition(
    name="file_read",
    description="Read a file from the agent workspace. Path is relative to the workspace root.",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Relative file path"}},
        "required": ["path"],
    },
    handler=_file_read,
))

tool_registry.register(ToolDefinition(
    name="file_write",
    description="Write content to a file in the agent workspace. Creates parent directories automatically.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative file path"},
            "content": {"type": "string", "description": "Content to write"},
        },
        "required": ["path", "content"],
    },
    handler=_file_write,
))


# ---------------------------------------------------------------------------
# bash_exec
# ---------------------------------------------------------------------------

async def _bash_exec(command: str, timeout_seconds: int = 30) -> str:
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(_workspace()),
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
    description="Execute a bash command in the workspace directory. Returns stdout+stderr and exit code.",
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run"},
            "timeout_seconds": {"type": "integer", "description": "Timeout in seconds (default 30)", "default": 30},
        },
        "required": ["command"],
    },
    handler=_bash_exec,
))


# ---------------------------------------------------------------------------
# python_exec
# ---------------------------------------------------------------------------

async def _python_exec(code: str, timeout_seconds: int = 30) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", "-c", code,
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
            "timeout_seconds": {"type": "integer", "description": "Timeout in seconds (default 30)", "default": 30},
        },
        "required": ["code"],
    },
    handler=_python_exec,
))


# ---------------------------------------------------------------------------
# Stub tools — placeholders for tools that require external integrations
# ---------------------------------------------------------------------------

def _make_stub(tool_name: str, description: str, required_params: list[str]) -> ToolDefinition:
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
    )


_STUBS = [
    ("arxiv_search", "Search academic papers on arXiv", ["query"]),
    ("sql_query", "Execute a SQL query against a configured database", ["query"]),
    ("chart_gen", "Generate a chart from data and return a file path", ["data", "chart_type"]),
    ("csv_parse", "Parse a CSV file and return structured data", ["path"]),
    ("spell_check", "Check spelling and grammar in a text passage", ["text"]),
    ("readability_score", "Compute readability metrics for a passage", ["text"]),
    ("tone_analyzer", "Analyze the tone and sentiment of a passage", ["text"]),
    ("dependency_graph", "Build a dependency graph from a task list", ["tasks"]),
    ("timeline_gen", "Generate a project timeline from phases", ["phases"]),
    ("risk_model", "Produce a risk matrix for a set of risks", ["risks"]),
    ("lint", "Run a linter on source code", ["code", "language"]),
    ("test_runner", "Run a test suite and return results", ["test_path"]),
]

for _name, _desc, _params in _STUBS:
    tool_registry.register(_make_stub(_name, _desc, _params))
