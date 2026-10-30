"""Read-time context view: what the model sees each turn, derived from full history.

The agent loop keeps the complete message history — it is what the session log
records, what ``AgentResult.messages`` returns and what a resume continues from.
Each LLM request is built from ``ContextSnipper.project(messages)`` instead: once
a turn's prompt grows past ``trigger_tokens``, the contents of older bulky
``tool_result`` blocks are replaced with a stub that names their ``tool_use_id``,
and the model can fetch any of them back with the ``recall_tool_result`` tool.

Design constraints:

- **Never mutate history.** Only the view is shortened. Assistant turns are never
  touched (thinking signatures must round-trip, and an edited ``tool_use.input``
  reads to the model as a truncated request it then retries).
- **Prompt-cache friendly.** The snip boundary only moves forward, and only when
  the trigger fires, so between snips the projected prefix is byte-identical and
  stays cached; each snip costs one cache rebuild, not one per turn.
- **No stale size reads.** After moving the boundary, the snipper waits for usage
  from a call made *with* the new view before deciding again — otherwise the
  pre-snip size would trigger another snip on the very next turn.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

RECALL_TOOL_NAME = "recall_tool_result"

RECALL_TOOL_PARAM: dict[str, Any] = {
    "name": RECALL_TOOL_NAME,
    "description": (
        "Return the full content of an earlier tool result that was omitted from your "
        "context to save space. Use the tool_use_id shown in the omission note. Only "
        "call this when you actually need the omitted details again."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tool_use_id": {"type": "string", "description": "The id from the omission note."},
        },
        "required": ["tool_use_id"],
    },
}


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") if isinstance(b, dict) and b.get("type") == "text" else json.dumps(b)
            for b in content
        )
    return json.dumps(content)


def _stub(tool_use_id: str, chars: int) -> str:
    return (
        f"[Earlier tool result omitted to save context ({chars:,} chars). "
        f'Call {RECALL_TOOL_NAME}(tool_use_id="{tool_use_id}") if you need it again.]'
    )


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough prompt size (~4 chars/token) for when no API usage is available yet."""
    return len(json.dumps(messages)) // 4


def _is_tool_result_turn(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return (
        message.get("role") == "user"
        and isinstance(content, list)
        and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    )


@dataclass(frozen=True)
class SnipEvent:
    boundary: int        # tool results in messages[:boundary] are now stubbed
    snipped: int         # tool_result blocks newly stubbed by this event
    freed_chars: int
    prompt_tokens: int   # the measurement that triggered it


class ContextSnipper:
    """Per-invocation snip state for one agent loop."""

    def __init__(self, trigger_tokens: int, keep_recent: int, min_chars: int) -> None:
        self.trigger_tokens = trigger_tokens
        self.keep_recent = keep_recent
        self.min_chars = min_chars
        self.boundary = 0
        # Iteration whose LLM call first used the current boundary; decisions before
        # a fresh measurement would act on a stale (pre-snip) prompt size.
        self._boundary_since_iteration = 0

    @property
    def enabled(self) -> bool:
        return self.trigger_tokens > 0

    def _snippable(self, block: Any) -> bool:
        return (
            isinstance(block, dict)
            and block.get("type") == "tool_result"
            and len(_content_text(block.get("content"))) >= self.min_chars
        )

    def project(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The view sent to the model; *messages* itself is never modified."""
        if self.boundary == 0:
            return messages
        view = list(messages)
        for i in range(min(self.boundary, len(messages))):
            msg = messages[i]
            if not _is_tool_result_turn(msg):
                continue
            blocks = [
                {**b, "content": _stub(b.get("tool_use_id", ""), len(_content_text(b.get("content"))))}
                if self._snippable(b) else b
                for b in msg["content"]
            ]
            view[i] = {**msg, "content": blocks}
        return view

    def maybe_snip(
        self,
        messages: list[dict[str, Any]],
        prompt_tokens: int | None,
        iteration: int,
    ) -> SnipEvent | None:
        """Move the boundary forward if the last measured prompt exceeded the trigger.

        *prompt_tokens* is the full prompt size of the call made at *iteration*
        (input + cache read + cache write), or None before the first call, in which
        case the size of the current view is estimated.
        """
        if not self.enabled:
            return None
        if prompt_tokens is None:
            prompt_tokens = estimate_tokens(self.project(messages))
        elif iteration < self._boundary_since_iteration:
            return None  # measured before the current boundary took effect
        if prompt_tokens <= self.trigger_tokens:
            return None

        result_turns = [i for i, m in enumerate(messages) if _is_tool_result_turn(m)]
        if len(result_turns) <= self.keep_recent:
            return None
        new_boundary = result_turns[-self.keep_recent] if self.keep_recent else len(messages)
        if new_boundary <= self.boundary:
            logger.warning(
                "Prompt is %d tokens (trigger %d) but no older tool results are left to omit",
                prompt_tokens, self.trigger_tokens,
            )
            return None

        snipped = freed = 0
        for i in result_turns:
            if self.boundary <= i < new_boundary:
                for b in messages[i]["content"]:
                    if self._snippable(b):
                        snipped += 1
                        freed += len(_content_text(b.get("content")))
        self.boundary = new_boundary
        self._boundary_since_iteration = iteration + 1
        if snipped == 0:
            return None
        return SnipEvent(new_boundary, snipped, freed, prompt_tokens)


def recall(messages: list[dict[str, Any]], tool_use_id: str) -> str:
    """Full original content of an earlier tool result, from the unsnipped history."""
    for msg in messages:
        if not _is_tool_result_turn(msg):
            continue
        for b in msg["content"]:
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") == tool_use_id:
                return _content_text(b.get("content"))
    return f"No earlier tool result with tool_use_id {tool_use_id!r} exists in this task's history."
