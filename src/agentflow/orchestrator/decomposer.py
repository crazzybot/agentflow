"""Task decomposer — expands subtasks using per-manifest decomposition prompts.

Each decomposition runs a ReAct loop with read-only tools so the decomposer can
explore the workspace before deciding how to split the task.
"""
from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from agentflow.config import settings
from agentflow.core.models import AgentManifest, AgentStatus, ExecutionPlan, Subtask, TaskContext, TaskEnvelope
from agentflow.core.registry import AgentRegistry
from agentflow.llm import LLMClient

if TYPE_CHECKING:
    from agentflow.orchestrator.stream import StreamEmitter

logger = logging.getLogger(__name__)


# Tools the decomposer is allowed to use — read-only exploration only.
# bash_exec_readonly enforces the allowlist at the tool level so the decomposer
# can run find/grep/ls without risking writes or arbitrary code execution.
_DECOMPOSER_TOOLS = frozenset({"file_read", "bash_exec_readonly"})


def _extract_context_block(text: str) -> str:
    """Return the content of the first <decomposer_context>…</decomposer_context> block, or ''."""
    m = re.search(r"<decomposer_context>([\s\S]*?)</decomposer_context>", text)
    return m.group(1).strip() if m else ""


def _strip_context_block(text: str) -> str:
    """Remove any <decomposer_context>…</decomposer_context> blocks so _extract_json_array
    is not confused by brackets inside the context prose."""
    return re.sub(r"<decomposer_context>[\s\S]*?</decomposer_context>", "", text).strip()


def _extract_json_array(text: str) -> str:
    """Return the first JSON array found in *text*, stripping any surrounding prose or fences."""
    # Strip a leading ```[language] fence and trailing ``` if present
    fenced = re.search(r"```(?:\w+)?\s*([\s\S]*?)```", text)
    if fenced:
        text = fenced.group(1)

    # Find the outermost [...] span
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        return text[start : end + 1]

    return text.strip()


async def decompose_subtask(
    subtask: Subtask,
    manifest: AgentManifest,
    run_id: str,
    client: LLMClient,
    emitter: "StreamEmitter",
    task: str = "",
    user_context: dict | None = None,
) -> tuple[list[Subtask], str]:
    """Run a ReAct loop to decompose *subtask* using read-only tools.

    Returns ``(subtasks, context)`` where *subtasks* is the expanded list of
    micro-subtasks (or ``[subtask]`` if already small or if decomposition fails)
    and *context* is the synthesised workspace summary from the decomposer's
    ``<decomposer_context>`` block (empty string when absent).
    """
    from agentflow.agents.agent import Agent

    # Intersect with the read-only allowlist so the decomposer can explore the
    # workspace but cannot execute code or write files.
    allowed_tools = [t for t in manifest.tools if t in _DECOMPOSER_TOOLS]

    decomposer_manifest = AgentManifest(
        agent_id=f"{manifest.agent_id}.decomposer",
        domain=manifest.domain,
        tools=allowed_tools,
        mcp_servers=manifest.mcp_servers,
        system_prompt=manifest.decomposition_prompt or "",  # guaranteed non-None at call site
        max_iterations=settings.decomposer_max_iterations,
    )

    instruction_parts = []
    if task:
        instruction_parts.append(f"Top-level task:\n{task}")
    instruction_parts.append(
        f'Original subtask id: "{subtask.id}"\n'
        f'Agent: "{subtask.agent_id}"\n'
        f'Instruction:\n{subtask.instruction}'
    )
    envelope = TaskEnvelope(
        parent_run_id=run_id,
        agent_id=decomposer_manifest.agent_id,
        instruction="\n\n".join(instruction_parts),
        context=TaskContext(user_context=user_context or {}),
    )

    logger.info("[decomposer] Decomposing subtask %s via ReAct loop", subtask.id)
    result = await Agent(decomposer_manifest, client).run(envelope, emitter)

    if result.status == AgentStatus.failed:
        logger.warning("[decomposer] Decomposition loop failed for %s — keeping original", subtask.id)
        return [subtask], ""

    raw_text = result.output.text
    context = _extract_context_block(raw_text)
    json_text = _strip_context_block(raw_text)
    raw = _extract_json_array(json_text)

    try:
        items = json.loads(raw)
        if not isinstance(items, list) or len(items) <= 1:
            return [subtask], context

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
        return micro, context
    except (json.JSONDecodeError, KeyError) as exc:
        logger.warning(
            "[decomposer] Could not parse decomposition for %s: %s — keeping original.\nRaw output was:\n%s",
            subtask.id, exc, result.output.text[:500],
        )
        return [subtask], context


async def expand_plan(
    plan: ExecutionPlan,
    registry: AgentRegistry,
    client: LLMClient,
    emitter: "StreamEmitter",
    task: str = "",
    user_context: dict | None = None,
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

        micro, _ = await decompose_subtask(subtask, manifest, plan.run_id, client, emitter, task=task, user_context=user_context)
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
