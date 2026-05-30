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
      "agentId": "ResearchAgent",
      "instruction": "...",
      "dependsOn": [],
      "expectedOutput": "..."
    }
  ]
}

Domain routing rules — always apply when selecting agents:
- Technical analysis tasks (computing SMA, EMA, RSI, MACD, Bollinger Bands, ATR, or
  any price-derived indicator from OHLCV data) → assign to CodeAgent; instruct it to
  call read_skill(skill="technical-analysis") at the start of the task.
- Fundamental analysis tasks (DCF modelling, ratio analysis P/E EV/EBITDA ROE, earnings
  quality, valuation multiples) → assign to FinancialAnalystAgent.
- Equity research tasks (sourcing SEC filings, earnings transcripts, analyst estimates,
  investment thesis construction) → assign to ResearchAgent; instruct it to call
  read_skill(skill="equity-research") at the start of the task.
- General API or library documentation research → assign to ResearchAgent with no skill
  loading instruction.
- Project structure planning, dependency mapping, or risk analysis → assign to PlannerAgent;
  instruct it to use its domain knowledge and not search for established best practices.

Parallelism rules — minimise wall-clock time:
- Set dependsOn: [] for any subtask that does not genuinely need data produced by another
  subtask. Two subtasks that both depend on the same earlier task may — and should — run
  in parallel with each other.
- Only add a dependency when a subtask actually consumes output produced by the prior
  subtask (e.g. reads a file it wrote, or needs its structured result).
- Minimise the critical path length: prefer breadth over depth in the dependency graph.

Completeness verification — apply after every CodeAgent file-generation subtask:
- After any subtask that asks CodeAgent to produce N named output files, add a follow-up
  verification subtask (also assigned to CodeAgent) with this instruction pattern:
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
