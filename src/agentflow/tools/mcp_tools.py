"""MCP server connectivity — wraps remote MCP tools as ToolDefinitions.

Usage (inside an async context):

    async with mcp_session(config) as tools:
        # tools: list[ToolDefinition] — handlers keep the session alive
        ...
"""
from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator

from agentflow.tools.registry import ToolDefinition

if TYPE_CHECKING:
    from agentflow.core.models import MCPServerConfig

logger = logging.getLogger(__name__)

try:
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False
    logger.warning("'mcp' package not found — MCP server connectivity is disabled")


# Anthropic tool names must match ^[a-zA-Z0-9_-]{1,128}$
_INVALID_TOOL_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize_tool_name(name: str) -> str:
    """Map any character outside [a-zA-Z0-9_-] to underscore and cap at 128 chars."""
    return _INVALID_TOOL_CHARS.sub("_", name)[:128]


def _make_mcp_handler(session: Any, mcp_name: str):
    """mcp_name is the original name sent to the MCP server (may contain dots, etc.)."""
    async def handler(**kwargs: Any) -> str:
        try:
            result = await session.call_tool(mcp_name, kwargs)
            parts = [
                block.text if hasattr(block, "text") else str(block)
                for block in result.content or []
            ]
            return "\n".join(parts) if parts else "(empty response)"
        except Exception as exc:
            return f"MCP tool {mcp_name!r} error: {exc}"

    return handler


def _build_tool_defs(tools_result: Any, session: Any, server_name: str) -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name=_sanitize_tool_name(t.name),  # Anthropic-safe name
            description=t.description or f"MCP tool from {server_name}",
            input_schema=t.inputSchema or {"type": "object", "properties": {}},
            handler=_make_mcp_handler(session, t.name),  # original name for MCP call
        )
        for t in tools_result.tools
    ]


@asynccontextmanager
async def mcp_session(config: "MCPServerConfig") -> AsyncIterator[list[ToolDefinition]]:
    """Async context manager — yields ToolDefinitions backed by a live MCP session."""
    if not _MCP_AVAILABLE:
        logger.warning("Skipping MCP server %r — mcp package not installed", config.name)
        yield []
        return

    if config.transport == "stdio":
        async with _stdio_session(config) as tools:
            yield tools
        return

    # SSE transport (default)
    logger.info("Connecting to MCP server %r at %s", config.name, config.url)
    launched = False
    try:
        async with sse_client(config.url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_result = await session.list_tools()
                tool_defs = _build_tool_defs(tools_result, session, config.name)
                logger.info("Loaded %d tools from MCP server %r", len(tool_defs), config.name)
                launched = True
                yield tool_defs
    except Exception as exc:
        if not launched:
            # Connection / init failed — yield an empty tool list as graceful fallback.
            logger.error("Failed to connect to MCP server %r: %s", config.name, exc)
            yield []
        else:
            # Exception came from inside the agent body — propagate it; do not yield again.
            raise


def _stdio_env(config: "MCPServerConfig") -> dict[str, str]:
    """Return the env overrides for a stdio MCP server process.

    Only variables the manifest opts into are forwarded: `env_passthrough` names
    copied from the API process, then literal `env` values on top. The mcp SDK
    merges this onto its own safe default (PATH, HOME, USER, ...), so credentials
    such as ANTHROPIC_API_KEY never reach the server unless explicitly listed.
    """
    passthrough = {k: os.environ[k] for k in config.env_passthrough if k in os.environ}
    return {**passthrough, **config.env}


@asynccontextmanager
async def _stdio_session(config: "MCPServerConfig") -> AsyncIterator[list[ToolDefinition]]:
    """Stdio transport — spawns the MCP server process and communicates via stdin/stdout."""
    if not config.command:
        logger.error("MCP server %r has stdio transport but no command specified", config.name)
        yield []
        return

    try:
        from mcp.client.stdio import StdioServerParameters, stdio_client
    except ImportError:
        logger.error("mcp.client.stdio not available — cannot use stdio transport for %r", config.name)
        yield []
        return

    params = StdioServerParameters(command=config.command, args=config.args, env=_stdio_env(config))

    logger.info("Launching stdio MCP server %r: %s %s", config.name, config.command, " ".join(config.args))
    launched = False
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_result = await session.list_tools()
                tool_defs = _build_tool_defs(tools_result, session, config.name)
                logger.info("Loaded %d tools from stdio MCP server %r", len(tool_defs), config.name)
                launched = True
                yield tool_defs
    except Exception as exc:
        if not launched:
            # Process launch / init failed — yield an empty tool list as graceful fallback.
            logger.error("Failed to launch stdio MCP server %r: %s", config.name, exc)
            yield []
        else:
            # Exception came from inside the agent body — propagate it; do not yield again.
            raise
