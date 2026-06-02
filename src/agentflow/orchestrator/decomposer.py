"""Task decomposer — expands subtasks using per-manifest decomposition prompts.

Each decomposition runs a full ReAct loop with the same tools as the target agent,
so the decomposer can explore the workspace before deciding how to split the task.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from agentflow.config import settings
from agentflow.core.models import AgentManifest, AgentStatus, ExecutionPlan, Subtask, TaskEnvelope
from agentflow.core.registry import AgentRegistry
from agentflow.llm import LLMClient

if TYPE_CHECKING:
    from agentflow.orchestrator.stream import StreamEmitter

logger = logging.getLogger(__name__)


async def decompose_subtask(
    subtask: Subtask,
    manifest: AgentManifest,
    run_id: str,
    client: LLMClient,
    emitter: "StreamEmitter",
) -> list[Subtask]:
    """Run a ReAct loop to decompose *subtask* using the agent's own tools.

    Returns the expanded list of micro-subtasks, or [subtask] if the task is
    already small enough or if the decomposition loop fails.
    """
    from agentflow.agents.agent import Agent

    decomposer_manifest = AgentManifest(
        agent_id=f"{manifest.agent_id}.decomposer",
        domain=manifest.domain,
        tools=manifest.tools,
        mcp_servers=manifest.mcp_servers,
        system_prompt=manifest.decomposition_prompt or "",  # guaranteed non-None at call site
        max_iterations=settings.decomposer_max_iterations,
    )

    envelope = TaskEnvelope(
        parent_run_id=run_id,
        agent_id=decomposer_manifest.agent_id,
        instruction=(
            f'Original subtask id: "{subtask.id}"\n'
            f'Agent: "{subtask.agent_id}"\n'
            f'Instruction:\n{subtask.instruction}'
        ),
    )

    logger.info("[decomposer] Decomposing subtask %s via ReAct loop", subtask.id)
    result = await Agent(decomposer_manifest, client).run(envelope, emitter)

    if result.status == AgentStatus.failed:
        logger.warning("[decomposer] Decomposition loop failed for %s — keeping original", subtask.id)
        return [subtask]

    raw = result.output.text.strip().strip("```").strip("json").strip()
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


async def expand_plan(
    plan: ExecutionPlan,
    registry: AgentRegistry,
    client: LLMClient,
    emitter: "StreamEmitter",
) -> ExecutionPlan:
    """Expand subtasks whose agent manifest declares a decomposition_prompt.

    Non-decomposable subtasks pass through unchanged. The dependency graph is
    rewired so that any subtask that depended on an expanded subtask now depends
    on the last micro-subtask in its expansion chain.
    """
    tail_id: dict[str, str] = {}
    expanded: list[Subtask] = []

    for subtask in plan.subtasks:
        manifest = registry.get(subtask.agent_id)
        if not manifest or not manifest.decomposition_prompt:
            expanded.append(subtask)
            tail_id[subtask.id] = subtask.id
            continue

        micro = await decompose_subtask(subtask, manifest, plan.run_id, client, emitter)
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
