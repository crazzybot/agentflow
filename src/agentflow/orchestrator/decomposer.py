"""Task decomposer — expands subtasks using per-manifest decomposition prompts."""
from __future__ import annotations

import json
import logging

from anthropic.types import TextBlock

from agentflow.config import settings
from agentflow.core.models import ExecutionPlan, Subtask
from agentflow.core.registry import AgentRegistry
from agentflow.llm import LLMClient

logger = logging.getLogger(__name__)


async def decompose_subtask(
    subtask: Subtask,
    decomposition_prompt: str,
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
        system=decomposition_prompt,
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


async def expand_plan(plan: ExecutionPlan, registry: AgentRegistry, client: LLMClient) -> ExecutionPlan:
    """Expand subtasks whose agent manifest declares a decomposition_prompt.

    Non-decomposable subtasks pass through unchanged. The dependency graph is
    rewired so that any subtask that depended on an expanded subtask now depends
    on the last micro-subtask in its expansion chain.
    """
    tail_id: dict[str, str] = {}
    expanded: list[Subtask] = []

    for subtask in plan.subtasks:
        manifest = registry.get(subtask.agent_id)
        decomposition_prompt = manifest.decomposition_prompt if manifest else None
        if not decomposition_prompt:
            expanded.append(subtask)
            tail_id[subtask.id] = subtask.id
            continue

        micro = await decompose_subtask(subtask, decomposition_prompt, client)
        expanded.extend(micro)
        tail_id[subtask.id] = micro[-1].id

    rewired: list[Subtask] = []
    for st in expanded:
        new_deps = [tail_id.get(dep, dep) for dep in st.depends_on]
        if new_deps != st.depends_on:
            rewired.append(st.model_copy(update={"depends_on": new_deps}))
        else:
            rewired.append(st)

    return ExecutionPlan(run_id=plan.run_id, subtasks=rewired)
