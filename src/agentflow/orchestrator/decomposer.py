"""Task decomposer — breaks large coding subtasks into smaller, focused micro-subtasks."""
from __future__ import annotations

import json
import logging

from anthropic.types import TextBlock

from agentflow.config import settings
from agentflow.core.models import ExecutionPlan, Subtask
from agentflow.llm import LLMClient

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a coding task decomposer. Given a single coding task instruction, split it into
a sequenced list of smaller, focused micro-subtasks that can each be completed within
15 tool calls by a software engineer agent.

Rules:
- Each micro-subtask must produce at most 3 files.
- Micro-subtasks must form a valid dependency chain (later tasks may depend on earlier ones).
- Preserve the original agent_id for all micro-subtasks.
- Use the same id prefix as the original subtask (e.g. "st_1" → "st_1_a", "st_1_b", ...).
- If the task is already small enough (≤3 files, single logical unit), return it unchanged
  as a single-element list.
- Do not add tasks that are not a decomposition of the original. Do not invent extra work.

Return ONLY a JSON array (no markdown fences):
[
  {
    "id": "st_1_a",
    "agentId": "CodeAgent",
    "instruction": "...",
    "dependsOn": [],
    "expectedOutput": "..."
  },
  ...
]
"""


async def decompose_coding_subtask(
    subtask: Subtask,
    client: LLMClient,
) -> list[Subtask]:
    """Return a list of micro-subtasks expanding *subtask*, or [subtask] if no split needed."""
    user_message = (
        f'Original subtask id: "{subtask.id}"\n'
        f'Agent: "{subtask.agent_id}"\n'
        f'Instruction:\n{subtask.instruction}'
    )

    logger.info("[decomposer] Decomposing subtask %s", subtask.id)
    response = await client.messages.create(
        model=settings.planner_model,
        max_tokens=2048,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    block = response.content[0]
    raw = block.text.strip().strip("```").strip("json").strip() if isinstance(block, TextBlock) else ""

    try:
        items = json.loads(raw)
        if not isinstance(items, list) or len(items) <= 1:
            return [subtask]

        micro: list[Subtask] = []
        micro_fraction = (subtask.budget_fraction / len(items)) if subtask.budget_fraction else None
        for item in items:
            # Carry over the original subtask's depends_on only on the first micro-subtask
            base_deps = subtask.depends_on if not micro else [micro[-1].id]
            micro.append(
                Subtask(
                    id=item["id"],
                    agent_id=item.get("agentId", subtask.agent_id),
                    instruction=item["instruction"],
                    depends_on=item.get("dependsOn", base_deps),
                    expected_output=item.get("expectedOutput", ""),
                    budget_fraction=micro_fraction,
                )
            )
        logger.info("[decomposer] Expanded %s → %d micro-subtasks", subtask.id, len(micro))
        return micro
    except (json.JSONDecodeError, KeyError) as exc:
        logger.warning("[decomposer] Could not parse decomposition for %s: %s — keeping original", subtask.id, exc)
        return [subtask]


async def expand_plan(plan: ExecutionPlan, coding_agent_ids: set[str], client: LLMClient) -> ExecutionPlan:
    """Expand all coding subtasks in *plan* using the decomposer.

    Non-coding subtasks and already-small subtasks pass through unchanged.
    The dependency graph is rewired so that any subtask that depended on
    an expanded subtask now depends on the last micro-subtask in its chain.
    """
    # Map original subtask id → last micro-subtask id (for rewiring downstream deps)
    tail_id: dict[str, str] = {}
    expanded: list[Subtask] = []

    for subtask in plan.subtasks:
        if subtask.agent_id not in coding_agent_ids:
            expanded.append(subtask)
            tail_id[subtask.id] = subtask.id
            continue

        micro = await decompose_coding_subtask(subtask, client)
        expanded.extend(micro)
        tail_id[subtask.id] = micro[-1].id

    # Rewire: replace any depends_on reference to an expanded original with its tail
    rewired: list[Subtask] = []
    for st in expanded:
        new_deps = [tail_id.get(dep, dep) for dep in st.depends_on]
        if new_deps != st.depends_on:
            rewired.append(st.model_copy(update={"depends_on": new_deps}))
        else:
            rewired.append(st)

    return ExecutionPlan(run_id=plan.run_id, subtasks=rewired)
