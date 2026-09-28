"""Final report compiler — aggregates subtask results into a markdown file."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from typing import Any

from agentflow.config import settings
from agentflow.core.models import AgentResult, AgentStatus, ExecutionPlan
from agentflow.llm import LLMClient, supports_adaptive_thinking

logger = logging.getLogger(__name__)

# Safety ceiling on characters taken from a single agent result. Set far above
# normal agent output so it never trims ordinary results (the old 8k cap silently
# cut real content from the final report); hitting it is logged loudly.
_MAX_RESULT_CHARS = 100_000

_TRUNCATION_NOTE = (
    "\n\n> **Note:** this report was cut off because it reached the output token limit "
    "(REPORT_MAX_TOKENS). Some sections may be missing."
)

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
    # Combine prose and structured JSON so the synthesizer sees the full output.
    # _parse_final_output splits the agent's final message into a prose preamble
    # (text) and an extracted JSON dict (structured); using text-or-structured loses
    # the structured content whenever a non-empty but minimal preamble is present.
    parts: list[str] = []
    if result.output.text:
        parts.append(result.output.text)
    if result.output.structured:
        parts.append(json.dumps(result.output.structured, indent=2))
    text = "\n\n".join(parts) if parts else ""
    if len(text) > _MAX_RESULT_CHARS:
        logger.warning(
            "Result for task %s is %d chars — truncating to %d for report synthesis",
            result.task_id, len(text), _MAX_RESULT_CHARS,
        )
        text = text[:_MAX_RESULT_CHARS] + "\n… [truncated]"
    return text


def _report_model() -> str:
    return (
        settings.report_model
        or settings.resolve_model_tier(settings.report_model_tier)
        or settings.reporter_model
    )


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
    model = _report_model()
    create_kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": settings.report_max_tokens,
        "system": _SYNTHESIS_PROMPT,
        "messages": [{"role": "user", "content": synthesis_input}],
    }
    if settings.report_thinking_effort and supports_adaptive_thinking(model):
        create_kwargs["output_config"] = {"effort": settings.report_thinking_effort}
    response = await client.messages.create(**create_kwargs)
    # Join every text block — with thinking enabled the first block is not text,
    # and a long answer may arrive split across several text blocks.
    report_body = "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    ).strip()
    if response.stop_reason == "max_tokens":
        logger.warning(
            "[%s] Report synthesis hit max_tokens=%d — report is truncated",
            run_id, settings.report_max_tokens,
        )
        report_body += _TRUNCATION_NOTE

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
