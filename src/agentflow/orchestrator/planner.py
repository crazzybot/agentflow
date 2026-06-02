"""LLM-based task planning — agentic ReAct loop that explores the workspace
before committing to an execution plan."""
from __future__ import annotations

import asyncio
import json
import logging

import anthropic
from anthropic.types import TextBlock

from agentflow.config import settings
from agentflow.core.models import ExecutionPlan, Subtask
from agentflow.core.registry import AgentRegistry
from agentflow.llm import LLMClient
from agentflow.tools import tool_registry

logger = logging.getLogger(__name__)

# Tools the planner is allowed to call during its exploration phase.
_PLANNER_TOOLS = ["file_read", "bash_exec", "web_search", "fetch_url"]

# Planner tool results are capped at this many chars to keep the context manageable.
_MAX_TOOL_RESULT_CHARS = 8_000

_SYSTEM_PROMPT_BASE = """\
You are an orchestration planner. You have read-only tools to explore the workspace
before you commit to a plan.

EXPLORATION PHASE
Use file_read and bash_exec (find, ls, grep — no writes) to understand the workspace:
- Actual file structure and counts relevant to the task
- Technologies, frameworks, and complexity present
- Whether the task is small (one agent pass) or large (multiple subtasks)
Limit yourself to 5-8 tool calls; stop as soon as you have enough context.

PLANNING PHASE
Once you have sufficient context, output your execution plan as the final message.
Use ONLY a JSON object — no markdown fences, no prose before or after:
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

Task scope rules — calibrate to actual workspace complexity (from your exploration):
- A single subtask must output at most 3 files. If more are needed, split by logical module.
- If the entire task can be completed in ≤15 tool calls by one agent, use ONE subtask.
- Only split into multiple subtasks when the work genuinely spans distinct phases or agents.
- Link split subtasks in dependency order only when a later subtask genuinely imports or
  consumes output from an earlier one; otherwise let them run in parallel.

Parallelism rules — minimise wall-clock time:
- Set dependsOn: [] for any subtask that does not need output from another.
- Only add a dependency when a subtask actually consumes output produced by the prior one.
- Minimise critical path length: prefer breadth over depth.

Completeness verification:
- After any file-generating subtask, add a verification subtask assigned to the same agent:
  "Verify that all required files from [prior id] exist: [list files].
   For each missing file, write it now. Return JSON with files_written and files_missing."
- The verification subtask must depend on the generation subtask.
- Only add "fix bugs" when all required files already exist and the problem is behaviour.
  If files are missing, say "write the missing files" instead.
"""

_BUDGET_ALLOCATION_INSTRUCTIONS = """
Budget allocation:
A total run budget in USD has been allocated for this task. Each subtask must include a
"budgetFraction" field: a float between 0 and 1 representing that subtask's share of the
total budget. All fractions across all subtasks must sum to exactly 1.0.
Allocation guidance:
- Give more budget to subtasks that require many tool calls, large file reads, or complex
  code generation (expect more input + output tokens).
- Give less budget to lightweight subtasks (single-file reads, short verifications).
- The JSON schema with budgetFraction:
{
  "subtasks": [
    {
      "id": "st_1",
      "agentId": "AgentId",
      "instruction": "...",
      "dependsOn": [],
      "expectedOutput": "...",
      "budgetFraction": 0.7
    }
  ]
}
"""


async def _call_planner_tool(block: anthropic.types.ToolUseBlock, tools: list) -> dict:
    tool_def = next((t for t in tools if t.name == block.name), None)
    if tool_def is None:
        result_text = f"Tool {block.name!r} is not available to the planner."
    else:
        try:
            if block.name in {t.name for t in tool_registry.all()}:
                result_text = await tool_registry.execute(block.name, block.input)
            else:
                result_text = await tool_def.handler(**block.input)
        except Exception as exc:
            result_text = f"Tool error: {exc}"

    if len(result_text) > _MAX_TOOL_RESULT_CHARS:
        result_text = result_text[:_MAX_TOOL_RESULT_CHARS] + "\n… [truncated]"

    return {
        "type": "tool_result",
        "tool_use_id": block.id,
        "content": result_text,
    }


async def create_plan(
    run_id: str,
    task: str,
    registry: AgentRegistry,
    client: LLMClient | anthropic.AsyncAnthropic,
    budget_usd: float | None = None,
    user_context: dict | None = None,
) -> ExecutionPlan:
    agent_summary = registry.summary()
    planner_tools = tool_registry.get_many(_PLANNER_TOOLS)
    anthropic_tools = [t.to_anthropic_param() for t in planner_tools]

    system_prompt = _SYSTEM_PROMPT_BASE
    if budget_usd is not None:
        system_prompt = system_prompt + _BUDGET_ALLOCATION_INSTRUCTIONS

    budget_note = f" (budget: ${budget_usd:.4f})" if budget_usd is not None else ""
    context_note = (
        f"\n\nUser Context:\n{json.dumps(user_context, indent=2)}" if user_context else ""
    )
    messages: list[dict] = [
        {"role": "user", "content": f'Task: "{task}"{budget_note}{context_note}\n\nAvailable Agents:\n{agent_summary}'}
    ]
    last_response = None

    logger.info("[%s] Starting agentic planner (max %d iterations)", run_id, settings.planner_max_iterations)

    for iteration in range(settings.planner_max_iterations):
        response = await client.messages.create(
            model=settings.planner_model,
            max_tokens=4096,
            system=system_prompt,
            messages=messages,  # type: ignore
            tools=anthropic_tools, # type: ignore
        )
        last_response = response
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            logger.info("[%s] Planner finished after %d iteration(s)", run_id, iteration + 1)
            break

        if response.stop_reason != "tool_use":
            logger.warning("[%s] Planner unexpected stop_reason %r", run_id, response.stop_reason)
            break

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        logger.info("[%s] Planner iteration %d: %d tool call(s): %s",
                    run_id, iteration + 1, len(tool_use_blocks),
                    [b.name for b in tool_use_blocks])

        tool_results = await asyncio.gather(
            *[_call_planner_tool(b, planner_tools) for b in tool_use_blocks]
        )
        messages.append({"role": "user", "content": list(tool_results)})
    else:
        logger.warning("[%s] Planner hit iteration limit (%d)", run_id, settings.planner_max_iterations)

    # Extract the JSON plan from the final assistant text block
    raw = ""
    if last_response is not None:
        for block in last_response.content:
            if isinstance(block, TextBlock):
                raw = block.text
                break
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
                budget_fraction=st.get("budgetFraction"),
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
                budget_fraction=1.0 if budget_usd is not None else None,
            )
        ]

    # Ensure fractions are set and sum to 1.0 whenever a run budget is provided.
    # The model may omit budgetFraction or return values that don't sum to 1.0.
    if budget_usd is not None and subtasks:
        n = len(subtasks)
        total = sum(st.budget_fraction or 0.0 for st in subtasks)
        if total < 0.01:
            # Model omitted fractions entirely — distribute equally.
            subtasks = [st.model_copy(update={"budget_fraction": 1.0 / n}) for st in subtasks]
        elif abs(total - 1.0) > 0.01:
            # Renormalize so fractions sum to exactly 1.0.
            subtasks = [
                st.model_copy(update={"budget_fraction": (st.budget_fraction or 0.0) / total})
                for st in subtasks
            ]

    return ExecutionPlan(run_id=run_id, subtasks=subtasks)
