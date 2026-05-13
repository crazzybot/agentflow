"""LLM-based task decomposition — produces a structured execution plan."""
from __future__ import annotations

import json
import logging
import uuid

import anthropic

from agentflow.config import settings
from agentflow.core.models import ExecutionPlan, Subtask
from agentflow.core.registry import AgentRegistry

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an orchestration planner. Given a task and a list of available agents,
produce a JSON execution plan. Never invent agents not in the registry.

Return ONLY a JSON object with this exact schema (no markdown fences):
{
  "subtasks": [
    {
      "id": "st_1",
      "agentId": "ResearchAgent",
      "instruction": "...",
      "dependsOn": [],
      "expectedOutput": "..."
    }
  ]
}
"""


async def create_plan(
    run_id: str,
    task: str,
    registry: AgentRegistry,
    client: anthropic.AsyncAnthropic,
) -> ExecutionPlan:
    agent_summary = registry.summary()
    user_message = f'Task: "{task}"\n\nAvailable Agents:\n{agent_summary}'

    logger.info("[%s] Calling planner LLM", run_id)
    response = await client.messages.create(
        model=settings.planner_model,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    raw = response.content[0].text
    try:
        plan_data = json.loads(raw)
        subtasks = [
            Subtask(
                id=st["id"],
                agent_id=st["agentId"],
                instruction=st["instruction"],
                depends_on=st.get("dependsOn", []),
                expected_output=st.get("expectedOutput", ""),
            )
            for st in plan_data["subtasks"]
        ]
    except (json.JSONDecodeError, KeyError) as exc:
        logger.warning("[%s] Planner returned unparseable JSON: %s — falling back", run_id, exc)
        subtasks = [
            Subtask(
                id="st_1",
                agent_id=registry.all()[0].agent_id if registry.all() else "ResearchAgent",
                instruction=task,
                depends_on=[],
                expected_output="task result",
            )
        ]

    return ExecutionPlan(run_id=run_id, subtasks=subtasks)
