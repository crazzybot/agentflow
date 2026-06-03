"""MCP server connectivity — wraps remote MCP tools as ToolDefinitions.

Usage (inside an async context):

    async with mcp_session(config) as tools:
        # tools: list[ToolDefinition] — handlers keep the session alive
        ...
"""
from __future__ import annotations

import logging
import os
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


def _make_mcp_handler(session: Any, tool_name: str):
    async def handler(**kwargs: Any) -> str:
        try:
            result = await session.call_tool(tool_name, kwargs)
            parts = []
            for block in result.content or []:
                if hasattr(block, "text"):
                    parts.append(block.text)
                else:
                    parts.append(str(block))
            return "\n".join(parts) if parts else "(empty response)"
        except Exception as exc:
            return f"MCP tool {tool_name!r} error: {exc}"

    return handler


def _build_tool_defs(tools_result: Any, session: Any, server_name: str) -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name=t.name,
            description=t.description or f"MCP tool from {server_name}",
            input_schema=t.inputSchema or {"type": "object", "properties": {}},
            handler=_make_mcp_handler(session, t.name),
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
    try:
        async with sse_client(config.url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_result = await session.list_tools()
                tool_defs = _build_tool_defs(tools_result, session, config.name)
                logger.info("Loaded %d tools from MCP server %r", len(tool_defs), config.name)
                yield tool_defs
    except Exception as exc:
        logger.error("Failed to connect to MCP server %r: %s", config.name, exc)
        yield []


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

    # Merge caller-supplied env on top of the current process environment so the
    # subprocess inherits PATH and other essentials while allowing overrides.
    merged_env = {**os.environ, **config.env} if config.env else None
    params = StdioServerParameters(command=config.command, args=config.args, env=merged_env)

    logger.info("Launching stdio MCP server %r: %s %s", config.name, config.command, " ".join(config.args))
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_result = await session.list_tools()
                tool_defs = _build_tool_defs(tools_result, session, config.name)
                logger.info("Loaded %d tools from stdio MCP server %r", len(tool_defs), config.name)
                yield tool_defs
    except Exception as exc:
        logger.error("Failed to launch stdio MCP server %r: %s", config.name, exc)
        yield []
