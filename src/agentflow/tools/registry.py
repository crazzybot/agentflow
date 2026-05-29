"""ToolDefinition and ToolRegistry — the single source of truth for all tools."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


class ToolImpact(str, Enum):
    read_only = "read_only"  # no side effects; reads data from network or workspace
    write     = "write"      # creates or modifies files in the workspace
    execute   = "execute"    # runs arbitrary code or queries; broadest side-effect surface


@dataclass
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Awaitable[str]]
    impact: ToolImpact = ToolImpact.read_only

    def to_anthropic_param(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool
        logger.debug("Registered tool: %s", tool.name)

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def get_many(self, names: list[str]) -> list[ToolDefinition]:
        """Return definitions for the named tools; silently skip unknowns."""
        result = []
        for name in names:
            tool = self._tools.get(name)
            if tool:
                result.append(tool)
            else:
                logger.debug("Tool %r not found in registry — skipping", name)
        return result

    def all(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    async def execute(self, name: str, input_data: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"Unknown tool: {name!r}"
        try:
            return await tool.handler(**input_data)
        except TypeError as exc:
            return f"Tool call error for {name!r}: {exc}"
        except Exception as exc:
            logger.exception("Tool %r raised an unexpected error", name)
            return f"Tool {name!r} failed: {exc}"


# Global registry — populated by builtin.py at import time
tool_registry = ToolRegistry()
