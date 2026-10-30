"""Independent verification of a subtask's acceptance criteria.

A subtask's own agent reports its success itself; for subtasks that carry
``acceptance_criteria`` the engine also asks a separate, read-only agent to check
each criterion against the workspace before the subtask counts as done. The
verifier only gathers evidence (``file_read``, ``bash_exec_readonly``) — it cannot
change the workspace, and it never sees the producing agent's conversation, only
its reported output and the files it wrote.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from agentflow.agents.agent import Agent
from agentflow.config import settings
from agentflow.core.models import (
    AgentManifest,
    AgentResult,
    AgentStatus,
    CriterionCheck,
    IterationLimitAction,
    Subtask,
    TaskEnvelope,
    Verification,
)

if TYPE_CHECKING:
    from agentflow.llm import LLMClient
    from agentflow.orchestrator.stream import StreamEmitter

logger = logging.getLogger(__name__)

_VERIFIER_TOOLS = ["file_read", "bash_exec_readonly"]

# The reported output is evidence, not ground truth; cap it so a verbose agent
# cannot crowd out the verifier's own exploration.
_MAX_REPORTED_OUTPUT_CHARS = 6_000

_SYSTEM_PROMPT = """\
You are an independent verifier. Another agent has just finished a task and claims it
is done. Decide, criterion by criterion, whether the work actually meets its
acceptance criteria.

Rules:
- Check the workspace yourself with file_read and bash_exec_readonly. Treat the
  agent's reported output as a claim to verify, never as evidence on its own.
- A criterion is met only if you found concrete evidence for it (a file, its content,
  a passing structure). If you cannot confirm it, it is NOT met.
- Be specific in evidence: name files, lines, or what is missing.
- Keep exploration short (a few tool calls per criterion).

Final message: ONLY a JSON object, no prose or fences:
{
  "checks": [
    {"criterion": "<criterion text>", "met": true, "evidence": "<what you found>"}
  ],
  "summary": "<one sentence>"
}
Include exactly one entry per criterion, in the given order.
"""


def _instruction(subtask: Subtask, result: AgentResult) -> str:
    reported = result.output.text or ""
    if result.output.structured:
        reported += ("\n\n" if reported else "") + json.dumps(result.output.structured, indent=2)
    if len(reported) > _MAX_REPORTED_OUTPUT_CHARS:
        reported = reported[:_MAX_REPORTED_OUTPUT_CHARS] + "\n… [reported output truncated]"
    criteria = "\n".join(f"{i}. {c}" for i, c in enumerate(subtask.acceptance_criteria, 1))
    files = "\n".join(f"- {p}" for p in result.files_written) or "(none reported)"
    return (
        f"<task>\n{subtask.instruction}\n</task>\n\n"
        f"<acceptance_criteria>\n{criteria}\n</acceptance_criteria>\n\n"
        f"<files_written_by_agent>\n{files}\n</files_written_by_agent>\n\n"
        f"<agent_reported_output>\n{reported or '(empty)'}\n</agent_reported_output>"
    )


def _parse_verdict(subtask: Subtask, structured: dict) -> Verification:
    raw = structured.get("checks")
    if not isinstance(raw, list) or len(raw) != len(subtask.acceptance_criteria):
        return Verification(passed=None, summary="Verifier returned no usable per-criterion verdict")
    checks = []
    for criterion, entry in zip(subtask.acceptance_criteria, raw):
        if not isinstance(entry, dict) or not isinstance(entry.get("met"), bool):
            return Verification(passed=None, summary="Verifier returned a malformed check")
        # Keep the planner's wording: the verifier may paraphrase it.
        checks.append(CriterionCheck(criterion=criterion, met=entry["met"], evidence=str(entry.get("evidence", ""))))
    return Verification(
        passed=all(c.met for c in checks),
        checks=checks,
        summary=str(structured.get("summary", "")),
    )


async def verify_subtask(
    run_id: str,
    subtask: Subtask,
    result: AgentResult,
    client: LLMClient,
    emitter: StreamEmitter,
) -> tuple[Verification, AgentResult]:
    """Check *result* against *subtask*'s acceptance criteria.

    Returns the verdict and the verifier's own AgentResult (for cost accounting).
    Never raises for a verifier malfunction: that is an inconclusive verdict.
    """
    manifest = AgentManifest(
        agent_id=f"{subtask.agent_id}.verifier",
        domain="Verification",
        tools=_VERIFIER_TOOLS,
        system_prompt=_SYSTEM_PROMPT,
        max_iterations=settings.verifier_max_iterations,
        on_iteration_limit=IterationLimitAction.finalize,
        model_tier=settings.verifier_model_tier,
        thinking_effort="low",
    )
    envelope = TaskEnvelope(
        parent_run_id=run_id,
        agent_id=manifest.agent_id,
        instruction=_instruction(subtask, result),
    )
    verifier_result = await Agent(manifest, client).run(envelope, emitter)
    if verifier_result.status == AgentStatus.failed:
        logger.warning("[%s] Verifier for %s failed: %s", run_id, subtask.id, verifier_result.error)
        return Verification(passed=None, summary=f"Verifier failed: {verifier_result.error}"), verifier_result
    verdict = _parse_verdict(subtask, verifier_result.output.structured or {})
    if verdict.passed is None:
        logger.warning("[%s] Inconclusive verification for %s: %s", run_id, subtask.id, verdict.summary)
    return verdict, verifier_result


def repair_message(verdict: Verification) -> str:
    """The user turn that hands the verifier's findings back to the producing agent."""
    lines = "\n".join(f"- {c.criterion}\n  Finding: {c.evidence or 'not confirmed'}" for c in verdict.unmet)
    return (
        "An independent check of your work found these acceptance criteria NOT met:\n"
        f"{lines}\n\n"
        "Fix each of them in the workspace, verify your fixes, then give your final "
        "answer again in the same format as before."
    )
