"""LLM-based task planning — agentic ReAct loop that explores the workspace
before committing to an execution plan."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import anthropic
from anthropic.types import TextBlock

from agentflow.config import settings
from agentflow.core.models import ExecutionPlan, SSEEventType, Subtask
from agentflow.core.registry import AgentRegistry
from agentflow.llm import LLMClient
from agentflow.tools import tool_registry

if TYPE_CHECKING:
    from agentflow.orchestrator.stream import StreamEmitter

logger = logging.getLogger(__name__)

# Tools the planner is allowed to call during its exploration phase.
_PLANNER_TOOLS = ["file_read", "bash_exec_readonly", "web_search", "fetch_url"]

# Planner tool results are capped at this many chars to keep the context manageable.
_MAX_TOOL_RESULT_CHARS = 8_000

_SYSTEM_PROMPT_BASE = """\
You are an orchestration planner. You have read-only tools to explore the workspace
before you commit to a plan.

EXPLORATION PHASE
Use file_read and bash_exec_readonly (find, ls, grep, wc, diff, etc.) to understand the workspace:
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
- Agents that list skills have the full skill documentation pre-loaded in their system
  prompt. Do NOT prefix instructions with "Start by calling read_skill" — the agent
  already has the guidance available and does not need to fetch it.

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

Context inheritance — critical when writing downstream instructions:
- When subtask B has dependsOn: ["st_N"], the system automatically passes the full
  conversation history from st_N to B. This means B already has in its context any
  files that st_N wrote, any web/arxiv results it fetched, etc.
- Therefore: do NOT instruct B to read files that st_N produced. Instead write:
    "The [filename] has been prepared and its content is already in your context
     from the previous step — synthesise from context, do not re-read the file."
- Only instruct a downstream agent to read a file if it was NOT produced by its
  single upstream dependency (e.g. a pre-existing workspace file).

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
    emitter: "StreamEmitter | None" = None,
) -> ExecutionPlan:
    agent_summary = registry.summary()
    planner_tools = tool_registry.get_many(_PLANNER_TOOLS)
    anthropic_tools = [t.to_anthropic_param() for t in planner_tools]

    static_prompt = _SYSTEM_PROMPT_BASE
    if budget_usd is not None:
        static_prompt = static_prompt + _BUDGET_ALLOCATION_INSTRUCTIONS

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    system_blocks = [
        {"type": "text", "text": static_prompt, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": f"Current date and time: {now}"},
    ]

    budget_note = f" (budget: ${budget_usd:.4f})" if budget_usd is not None else ""

    # Separate prior-run context (only relevant to the planner) from any
    # additional user-supplied context that should appear as a JSON block.
    _PRIOR_RUN_KEYS = {"prior_run_id", "prior_task", "prior_report", "prior_subtask_outputs"}
    prior_run = {k: v for k, v in (user_context or {}).items() if k in _PRIOR_RUN_KEYS}
    extra_context = {k: v for k, v in (user_context or {}).items() if k not in _PRIOR_RUN_KEYS}

    parts: list[str] = [f'Task: "{task}"{budget_note}']

    if prior_run:
        prior_parts: list[str] = []
        if "prior_run_id" in prior_run:
            prior_parts.append(f'Prior run ID: {prior_run["prior_run_id"]}')
        if "prior_task" in prior_run:
            prior_parts.append(f'Prior task: "{prior_run["prior_task"]}"')
        if "prior_report" in prior_run:
            prior_parts.append(f'Prior report:\n---\n{prior_run["prior_report"]}\n---')
        if "prior_subtask_outputs" in prior_run:
            outputs = prior_run["prior_subtask_outputs"]
            lines = "\n".join(
                f'  [{i["agent_id"]}] {i["output"][:400]}{"…" if len(i["output"]) > 400 else ""}'
                for i in outputs
                if i.get("output")
            )
            if lines:
                prior_parts.append(f"Prior subtask outputs:\n{lines}")
        parts.append("\n".join(prior_parts))

    if extra_context:
        parts.append(f"User Context:\n{json.dumps(extra_context, indent=2)}")

    parts.append(f"Available Agents:\n{agent_summary}")

    messages: list[dict] = [{"role": "user", "content": "\n\n".join(parts)}]
    last_response = None

    logger.info("[%s] Starting agentic planner (max %d iterations)", run_id, settings.planner_max_iterations)

    if emitter is not None:
        emitter.emit(SSEEventType.agent_progress, agent_id="planner", message="Planning task...")

    for iteration in range(settings.planner_max_iterations):
        response = await client.messages.create(
            model=settings.planner_model,
            max_tokens=4096,
            system=system_blocks,
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

        if emitter is not None:
            tool_names = [b.name for b in tool_use_blocks]
            emitter.emit(
                SSEEventType.agent_progress,
                agent_id="planner",
                message=f"Exploring workspace: {', '.join(tool_names)}",
                turn_index=iteration + 1,
            )
            for block in response.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    emitter.emit(SSEEventType.agent_thought, agent_id="planner", message=block.text, turn_index=iteration + 1)

        tool_results = await asyncio.gather(
            *[_call_planner_tool(b, planner_tools) for b in tool_use_blocks]
        )
        messages.append({"role": "user", "content": list(tool_results)})
    else:
        logger.warning("[%s] Planner hit iteration limit (%d)", run_id, settings.planner_max_iterations)

    # Extract the JSON plan from the final assistant text block.
    # The model may wrap the object in prose or markdown fences; find the
    # outermost {...} span so those wrappers don't break parsing.
    raw = ""
    if last_response is not None:
        for block in last_response.content:
            if isinstance(block, TextBlock):
                raw = block.text
                break

    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        raw = raw[start : end + 1]

    logger.info("[%s] Planner raw output (%d chars): %s…", run_id, len(raw), raw[:300])

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
        logger.warning(
            "[%s] Planner returned unparseable JSON (%s) — falling back to first agent.\nRaw was:\n%s",
            run_id, exc, raw[:1000],
        )
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

    logger.info("[%s] Planner routing: %s", run_id, [(st.id, st.agent_id) for st in subtasks])

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
