"""Resuming an interrupted run from its session log.

``load_resumable()`` replays a run's log and refuses runs that cannot be resumed;
``ResumeState.from_snapshot()`` turns the replayed snapshot into what the engine
needs to pick the run back up: the logged plan, the scheduler's finished
subtasks, logged micro-plans, and — for subtasks that were mid-flight — the
transcript of their last agent invocation, trimmed to a valid resume point.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentflow.core.models import AgentResult, ExecutionPlan, Subtask
from agentflow.core.session import RunSnapshot, read_session, replay, session_path


class RunNotResumableError(Exception):
    """Raised when a run has no session log, never started, or already finished."""


def load_resumable(run_id: str) -> RunSnapshot:
    if not session_path(run_id).exists():
        raise RunNotResumableError(f"Run {run_id!r} has no session log to resume from")
    snap = replay(run_id, read_session(run_id))
    if not snap.task:
        raise RunNotResumableError(f"Run {run_id!r} has no run_started event")
    if not snap.resumable:
        raise RunNotResumableError(f"Run {run_id!r} already finished with status {snap.status!r}")
    return snap


def _trim_transcript(messages: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Cut a crashed invocation's history back to a point the model can continue from.

    A trailing assistant turn that requested tools is dropped: its tool results were
    never logged (the process died while the tools ran), and the API requires every
    tool_use to be answered. The model re-issues whatever it still needs; it sees the
    workspace as the interrupted tools left it.
    """
    msgs = list(messages)
    if msgs and msgs[-1].get("role") == "assistant":
        content = msgs[-1].get("content")
        if isinstance(content, list) and any(b.get("type") == "tool_use" for b in content):
            msgs.pop()
    # Only the opening instruction left: a fresh dispatch is equivalent and rebuilds
    # the envelope from current upstream results.
    if len(msgs) <= 1:
        return None
    return msgs


@dataclass
class ResumeState:
    plan: ExecutionPlan | None
    results: dict[str, AgentResult]
    total_cost_usd: float
    finished: dict[str, bool]
    micro_plans: dict[str, list[Subtask]]
    after_seq: int
    # subtask_id → (agent_id, messages); consumed once by take_transcript().
    transcripts: dict[str, tuple[str, list[dict[str, Any]]]] = field(default_factory=dict)

    @classmethod
    def from_snapshot(cls, snap: RunSnapshot) -> ResumeState:
        transcripts: dict[str, tuple[str, list[dict[str, Any]]]] = {}
        for subtask_id, task_id in snap.dispatched.items():
            if subtask_id in snap.finished:
                continue
            # The newest invocation for this dispatch holds the fullest history:
            # retries start over, continuations re-log their inherited prefix.
            candidates = [
                inv for inv in snap.invocations.values()
                if inv.task_id == task_id and not inv.finished and inv.agent_id
            ]
            if not candidates:
                continue
            last = candidates[-1]
            trimmed = _trim_transcript(last.messages)
            if trimmed is not None:
                transcripts[subtask_id] = (last.agent_id or "", trimmed)
        return cls(
            plan=snap.plan,
            results=dict(snap.results),
            total_cost_usd=snap.total_cost_usd,
            finished=dict(snap.finished),
            micro_plans=dict(snap.micro_plans),
            after_seq=snap.last_seq,
            transcripts=transcripts,
        )

    def take_transcript(self, subtask_id: str, agent_id: str) -> list[dict[str, Any]] | None:
        """Return the logged history to resume *subtask_id* with, at most once.

        A transcript from a different agent (a fallback was running) is discarded:
        the primary agent restarts cleanly instead of inheriting another agent's turns.
        """
        entry = self.transcripts.pop(subtask_id, None)
        if entry is None or entry[0] != agent_id:
            return None
        return entry[1]
