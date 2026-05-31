"""LLM-based task decomposition — produces a structured execution plan."""
from __future__ import annotations

import json
import logging
import uuid

import anthropic
from anthropic.types import TextBlock

from agentflow.config import settings
from agentflow.core.models import ExecutionPlan, Subtask
from agentflow.core.registry import AgentRegistry
from agentflow.llm import LLMClient

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an orchestration planner. Given a task and a list of available agents,
produce a JSON execution plan. Never invent agents not in the registry.
Never invent a task that isn't a decomposition of the original task. It's OK if
not all agents are used.

Return ONLY a JSON object with this exact schema (no markdown fences):
{
  "subtasks": [
    {
      "id": "st_1",
      "agentId": "AgentId",
      "instruction": "...",
      "dependsOn": [],
      "expectedOutput": "..."
    }
  ]
}

Agent selection — derive routing entirely from the agent list provided:
- Match each subtask to the agent whose domain and capabilities best fit the work.
- Prefer a specialist agent over a generalist when both could handle the task.
- If the selected agent lists one or more skills, identify which skill is most relevant
  to the subtask and begin the instruction with:
  'Start by calling read_skill(skill="<name>") to load the relevant guidance.'
  Only include this line when the agent has skills and the task falls within a skill's domain.

Task scope rules — apply to every file-producing subtask:
- A single file-producing subtask must output at most 3 files. If more are needed,
  split into separate subtasks by logical module or page.
- Link split subtasks in dependency order only when a later module genuinely imports
  from an earlier one; otherwise let them run in parallel.

Parallelism rules — minimise wall-clock time:
- Set dependsOn: [] for any subtask that does not genuinely need data produced by another
  subtask. Two subtasks that both depend on the same earlier task may — and should — run
  in parallel with each other.
- Only add a dependency when a subtask actually consumes output produced by the prior
  subtask (e.g. reads a file it wrote, or needs its structured result).
- Minimise the critical path length: prefer breadth over depth in the dependency graph.

Completeness verification — apply after every file-generating subtask:
- After any subtask that asks an agent to produce N named output files, add a follow-up
  verification subtask assigned to the same agent with this instruction pattern:
  "Verify that all required files from [prior subtask id] exist: [list every filename].
   For each missing file, write it now. Return JSON with files_written and files_missing."
- The verification subtask must depend on the generation subtask and must complete before
  any testing or debugging subtask starts.
- Only use the phrase "fix bugs" in an instruction when all required files already exist
  and the problem is incorrect behaviour. If files are missing, say "write the missing
  files" instead.
"""


async def create_plan(
    run_id: str,
    task: str,
    registry: AgentRegistry,
    client: LLMClient | anthropic.AsyncAnthropic,
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

    block = response.content[0]
    raw = block.text if isinstance(block, TextBlock) else ""
    # Clean up any ````json` fences if present
    raw = raw.strip().strip("```").strip("json").strip()

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
