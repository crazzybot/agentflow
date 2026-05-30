"""read_skill — built-in tool for loading skill guidance documents."""
from __future__ import annotations

from agentflow.core.skill_loader import skill_loader
from agentflow.tools.registry import ToolDefinition, ToolImpact, tool_registry


async def _read_skill(skill: str, topic: str = "general") -> str:
    return skill_loader.read(skill, topic)


tool_registry.register(ToolDefinition(
    name="read_skill",
    description=(
        "Load guidance from the skills library. "
        "Use topic=\"general\" to read the skill overview (SKILL.md), which lists available "
        "reference documents. Supply a reference document name for detailed guidance on a subtopic."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "Skill folder name, e.g. \"financial_analysis\"",
            },
            "topic": {
                "type": "string",
                "description": (
                    "\"general\" for the SKILL.md overview, "
                    "or a reference document name listed in the overview"
                ),
                "default": "general",
            },
        },
        "required": ["skill"],
    },
    handler=_read_skill,
    impact=ToolImpact.read_only,
))
