"""LLM-based task planning — delegates to an in-memory Agent so the planner
shares the same ReAct loop, tool-execution, prompt-caching, and SSE-event
infrastructure as every other agent in the system."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from agentflow.agents.agent import Agent
from agentflow.config import settings
from agentflow.core.models import (
    AgentManifest,
    AgentStatus,
    ExecutionPlan,
    Subtask,
    TaskEnvelope,
)
from agentflow.core.registry import AgentRegistry
from agentflow.llm import LLMClient

if TYPE_CHECKING:
    import anthropic
    from agentflow.orchestrator.stream import StreamEmitter

logger = logging.getLogger(__name__)

# Tools the planner is allowed to call during its exploration phase.
_PLANNER_TOOLS = ["file_read", "bash_exec_readonly", "web_search", "fetch_url"]

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


async def create_plan(
    run_id: str,
    task: str,
    registry: AgentRegistry,
    client: "LLMClient | anthropic.AsyncAnthropic",
    budget_usd: float | None = None,
    user_context: dict | None = None,
    emitter: "StreamEmitter",
) -> ExecutionPlan:
    # Build system prompt
    system_prompt = _SYSTEM_PROMPT_BASE
    if budget_usd is not None:
        system_prompt += _BUDGET_ALLOCATION_INSTRUCTIONS

    # Build instruction: task + optional prior-run context + extra context + agent roster.
    # The planner sees the full agent list so it can make informed routing decisions.
    _PRIOR_RUN_KEYS = {"prior_run_id", "prior_task", "prior_report", "prior_subtask_outputs"}
    prior_run = {k: v for k, v in (user_context or {}).items() if k in _PRIOR_RUN_KEYS}
    extra_context = {k: v for k, v in (user_context or {}).items() if k not in _PRIOR_RUN_KEYS}

    budget_note = f" (budget: ${budget_usd:.4f})" if budget_usd is not None else ""
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

    parts.append(f"Available Agents:\n{registry.summary()}")

    manifest = AgentManifest(
        agent_id="planner",
        domain="Orchestration",
        system_prompt=system_prompt,
        tools=_PLANNER_TOOLS,
        max_iterations=settings.planner_max_iterations,
        model=settings.planner_model,
    )
    envelope = TaskEnvelope(
        parent_run_id=run_id,
        agent_id="planner",
        instruction="\n\n".join(parts),
    )

    logger.info("[%s] Starting planner (max %d iterations)", run_id, settings.planner_max_iterations)
    result = await Agent(manifest, client).run(envelope, emitter)

    if result.status == AgentStatus.failed:
        raise RuntimeError(f"Planner agent failed: {result.error}")

    plan_data = result.output.structured
    if not plan_data or "subtasks" not in plan_data:
        raise RuntimeError(
            f"Planner did not produce a valid execution plan. "
            f"Output ({len(result.output.text)} chars):\n{result.output.text[:1000]}"
        )

    try:
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
    except (KeyError, TypeError) as exc:
        raise RuntimeError(
            f"Planner produced malformed subtask structure: {exc}. "
            f"Output:\n{result.output.text[:1000]}"
        ) from exc

    if not subtasks:
        raise RuntimeError("Planner returned an empty subtasks list.")

    logger.info("[%s] Planner routing: %s", run_id, [(st.id, st.agent_id) for st in subtasks])

    # Ensure fractions are set and sum to 1.0 whenever a run budget is provided.
    if budget_usd is not None and subtasks:
        n = len(subtasks)
        total = sum(st.budget_fraction or 0.0 for st in subtasks)
        if total < 0.01:
            subtasks = [st.model_copy(update={"budget_fraction": 1.0 / n}) for st in subtasks]
        elif abs(total - 1.0) > 0.01:
            subtasks = [
                st.model_copy(update={"budget_fraction": (st.budget_fraction or 0.0) / total})
                for st in subtasks
            ]

    return ExecutionPlan(run_id=run_id, subtasks=subtasks)
