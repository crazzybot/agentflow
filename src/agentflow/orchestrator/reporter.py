"""Final report compiler — aggregates subtask results into a markdown file."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from typing import Any

from agentflow.config import settings
from agentflow.core.models import AgentResult, AgentStatus, ExecutionPlan
from agentflow.llm import LLMClient

logger = logging.getLogger(__name__)

# Max characters to include from a single agent result. Keeps the synthesis
# prompt predictable in size regardless of how verbose individual agents are.
_MAX_RESULT_CHARS = 8_000

_SYNTHESIS_PROMPT = """\
You are a report writer. Given a user task and results produced by specialist agents,
write a clear, concise, human-readable report in Markdown.

Structure:
- Start with a short executive summary (2-4 sentences).
- Include a section per completed agent result, with a heading derived from the agent's role.
- If there is an "Incomplete Work" section in the input, include a corresponding section in
  the report that describes what was partially done and what remains — do not omit or hide it.
- If there is a "Failed Tasks" section in the input, include a brief note on what failed.
- End with a conclusion / key takeaways section.

Use proper Markdown formatting (headings, bullet points, tables where helpful).
Do not mention internal implementation details like agent IDs, task IDs, or run IDs.
Write for a non-technical reader who asked the original question.
"""


def _leaf_subtask_ids(plan: ExecutionPlan) -> set[str]:
    """Return IDs of subtasks that no other subtask depends on (terminal nodes)."""
    all_deps: set[str] = set()
    for st in plan.subtasks:
        all_deps.update(st.depends_on)
    leaves = {st.id for st in plan.subtasks if st.id not in all_deps}
    # Fall back to all subtasks if the plan has no dependency structure.
    return leaves if leaves else {st.id for st in plan.subtasks}


def _result_text(result: AgentResult) -> str:
    text = result.output.text or str(result.output.structured)
    if len(text) > _MAX_RESULT_CHARS:
        text = text[:_MAX_RESULT_CHARS] + "\n… [truncated]"
    return text


async def compile_report(
    run_id: str,
    task: str,
    plan: ExecutionPlan,
    all_results: dict[str, AgentResult],
    client: LLMClient,
    cost_summary: dict[str, Any] | None = None,
) -> str:
    """Synthesise results, write the report to disk, and return the file path."""
    leaf_ids = _leaf_subtask_ids(plan)

    # Only pass leaf-node results to the synthesizer. Intermediate results have
    # already been consumed by downstream agents, so resending them is redundant.
    synthesis_results = {
        tid: r
        for tid, r in all_results.items()
        if tid in leaf_ids and r.status == AgentStatus.success
    }
    partials = {tid: r for tid, r in all_results.items() if r.status == AgentStatus.partial}
    failed = {tid: r for tid, r in all_results.items() if r.status == AgentStatus.failed}

    # If no leaf succeeded, fall back to all successful results.
    if not synthesis_results:
        synthesis_results = {tid: r for tid, r in all_results.items() if r.status == AgentStatus.success}

    parts: list[str] = [f'Original task: "{task}"\n']
    for result in synthesis_results.values():
        parts.append(f"## {result.agent_id}\n\n{_result_text(result)}\n")

    if partials:
        parts.append("## Incomplete Work")
        parts.append(
            "The following subtasks hit their iteration limit and may have produced "
            "only partial output. What was completed is shown below.\n"
        )
        for result in partials.values():
            parts.append(f"### {result.agent_id} (incomplete)\n\n{_result_text(result)}\n")

    if failed:
        parts.append("## Failed Tasks")
        for result in failed.values():
            parts.append(f"- {result.agent_id}: {result.error or 'unknown error'}")

    synthesis_input = "\n".join(parts)

    logger.info(
        "[%s] Requesting report synthesis (leaf nodes: %s, ~%d chars)",
        run_id, sorted(leaf_ids), len(synthesis_input),
    )
    response = await client.messages.create(
        model=settings.reporter_model,
        max_tokens=2048,
        system=_SYNTHESIS_PROMPT,
        messages=[{"role": "user", "content": synthesis_input}],
    )
    report_body = next(
        (block.text for block in response.content if hasattr(block, "text")), "" # type: ignore
    ).strip()

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cost_line = ""
    if cost_summary:
        cache_note = ""
        if cost_summary.get("cache_read_tokens"):
            cache_note = f", {cost_summary['cache_read_tokens']:,} cache-read"
        cost_line = (
            f"**Cost:** ${cost_summary['cost_usd']:.4f} "
            f"({cost_summary['input_tokens']:,} input"
            f" + {cost_summary['output_tokens']:,} output"
            f"{cache_note} tokens)  \n"
        )
    header = (
        f"# Run Report\n\n"
        f"**Task:** {task}  \n"
        f"**Generated:** {ts}  \n"
        f"**Run ID:** {run_id}  \n"
        f"{cost_line}"
        f"\n---\n\n"
    )
    full_report = header + report_body

    run_dir = os.path.join(settings.runs_dir, run_id)
    os.makedirs(run_dir, exist_ok=True)
    report_path = os.path.join(run_dir, "report.md")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(full_report)

    logger.info("[%s] Report saved to %s", run_id, report_path)
    return report_path
