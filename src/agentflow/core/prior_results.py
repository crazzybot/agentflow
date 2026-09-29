"""Upstream-context builders shared by every RunContext backend.

Kept backend-agnostic so the in-memory and Redis contexts cannot drift apart in
what downstream agents see from their dependencies.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping

from agentflow.core.models import AgentResult


def build_prior_results(results: Mapping[str, AgentResult], dep_ids: Iterable[str]) -> dict[str, str]:
    """Return text output from completed dependencies.

    Combines prose (output.text) and structured JSON (output.structured) so
    downstream agents see the full upstream result.  _parse_final_output splits
    the agent's final message into a prose preamble and an extracted JSON dict;
    using text-or-structured loses the structured content whenever a non-empty
    but minimal preamble is present.
    """
    prior: dict[str, str] = {}
    for dep_id in dep_ids:
        if dep_id not in results:
            continue
        output = results[dep_id].output
        parts: list[str] = []
        if output.text:
            parts.append(output.text)
        if output.structured:
            parts.append(json.dumps(output.structured, indent=2))
        prior[dep_id] = "\n\n".join(parts)
    return prior


def build_upstream_artifacts(results: Mapping[str, AgentResult], dep_ids: Iterable[str]) -> dict[str, list[str]]:
    """Return file paths written by completed dependencies, keyed by task ID."""
    return {
        dep_id: results[dep_id].files_written
        for dep_id in dep_ids
        if dep_id in results and results[dep_id].files_written
    }
