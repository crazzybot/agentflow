"""Durable, append-only session log — the record a run can be rebuilt from.

Each run appends typed ``SessionEvent``s to ``<runs_dir>/<run_id>/session.jsonl``.
Unlike the SSE stream (a UI projection, persisted only when ``capture_events`` is
set), the session log is always written and carries everything needed to
reconstruct the run: its inputs, the plan, every subtask dispatch and stored
result, cost, human-input exchanges, and the full message history of every agent
invocation (planner, decomposer, subtasks, retries, continuations, fallbacks).

Reading is by slice (``RunSession.events()``); ``replay()`` folds a slice back
into a ``RunSnapshot``. The log is file-backed in both state backends: Redis keys
expire after ``redis_key_ttl``, so they cannot be the durable record, and every
replica already shares ``runs_dir`` for meta/report/followups.

The engine arms ``_current_session`` for the lifetime of a run; ``record()`` is a
no-op outside a run, so agents and tools never need a reference to the log.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Collection
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from agentflow.core.workspace import runs_root
from agentflow.core.models import AgentResult, ExecutionPlan, Subtask

logger = logging.getLogger(__name__)

SESSION_FILE = "session.jsonl"


class SessionEventType(StrEnum):
    run_started = "run_started"              # task, user_context, budget_usd, workspace
    plan_created = "plan_created"            # plan, direct
    subtask_dispatched = "subtask_dispatched"  # subtask, task_id, budget_usd
    subtask_decomposed = "subtask_decomposed"  # micro (list of subtasks)
    subtask_finished = "subtask_finished"    # ok — the scheduler's completed/failed decision
    subtask_verified = "subtask_verified"    # verification (Verification), repairs
    checkpoint_created = "checkpoint_created"  # sha, label, changed — workspace git snapshot
    workspace_restored = "workspace_restored"  # sha, paths — failed attempt's files rolled back
    result_stored = "result_stored"          # result (AgentResult without messages)
    cost_added = "cost_added"                # cost_usd
    human_input_requested = "human_input_requested"  # request
    human_input_received = "human_input_received"    # response, budget_usd
    agent_started = "agent_started"          # task_id, model, resumed
    message_appended = "message_appended"    # message (+ usage/stop_reason on assistant turns)
    message_discarded = "message_discarded"  # reason — drops the invocation's last message
    context_snipped = "context_snipped"      # boundary, snipped, freed_chars, prompt_tokens (view only)
    agent_finished = "agent_finished"        # status, cost_usd, tokens, error
    run_interrupted = "run_interrupted"      # reason — process died mid-run; resumable
    run_resumed = "run_resumed"              # after_seq, finished, resuming
    run_finished = "run_finished"            # status: complete | error | cancelled


class SessionEvent(BaseModel):
    run_id: str
    seq: int
    ts: int = Field(default_factory=lambda: int(time.time() * 1000))
    type: SessionEventType
    agent_id: str | None = None
    subtask_id: str | None = None
    invocation_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class RunSessionResponse(BaseModel):
    run_id: str
    events: list[SessionEvent]


def session_path(run_id: str) -> Path:
    return runs_root() / run_id / SESSION_FILE


def _read(path: Path) -> list[SessionEvent]:
    if not path.exists():
        return []
    events: list[SessionEvent] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            events.append(SessionEvent.model_validate_json(line))
        except ValueError:
            # Only a torn final line (process killed mid-write) is expected; anything
            # else means the file was edited or corrupted and the rest is suspect.
            if i == len(lines) - 1:
                logger.warning("Ignoring truncated last line of %s", path)
            else:
                raise
    return events


class RunSession:
    """Append/read handle for one run's session log."""

    def __init__(self, run_id: str, path: Path | None = None) -> None:
        self.run_id = run_id
        self._path = path or session_path(run_id)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Continue numbering when reopening an existing log (e.g. resuming a run).
        existing = _read(self._path)
        self._seq = existing[-1].seq if existing else 0

    async def append(
        self,
        type: SessionEventType,
        *,
        agent_id: str | None = None,
        subtask_id: str | None = None,
        invocation_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> SessionEvent:
        # Async so a networked backend can slot in behind the same call sites; the
        # file backend completes synchronously.
        return self.append_sync(
            type, agent_id=agent_id, subtask_id=subtask_id, invocation_id=invocation_id, data=data
        )

    def append_sync(
        self,
        type: SessionEventType,
        *,
        agent_id: str | None = None,
        subtask_id: str | None = None,
        invocation_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> SessionEvent:
        """Append from synchronous code (startup reconciliation)."""
        # No await between numbering and the write, so concurrent subtasks on the
        # event loop can never interleave or reorder lines.
        self._seq += 1
        event = SessionEvent(
            run_id=self.run_id,
            seq=self._seq,
            type=type,
            agent_id=agent_id,
            subtask_id=subtask_id,
            invocation_id=invocation_id,
            data=data or {},
        )
        with self._path.open("a", encoding="utf-8") as f:
            f.write(event.model_dump_json() + "\n")
        return event

    def events(
        self,
        *,
        after_seq: int = 0,
        types: Collection[SessionEventType] | None = None,
        invocation_id: str | None = None,
    ) -> list[SessionEvent]:
        return filter_events(_read(self._path), after_seq=after_seq, types=types, invocation_id=invocation_id)


def filter_events(
    events: list[SessionEvent],
    *,
    after_seq: int = 0,
    types: Collection[SessionEventType] | None = None,
    invocation_id: str | None = None,
) -> list[SessionEvent]:
    return [
        e for e in events
        if e.seq > after_seq
        and (types is None or e.type in types)
        and (invocation_id is None or e.invocation_id == invocation_id)
    ]


def read_session(run_id: str) -> list[SessionEvent]:
    return _read(session_path(run_id))


class SessionWriter(Protocol):
    """Anything record() can append to: a RunSession, or a worker's relay back to
    the engine that owns the run's log (orchestrator/workers.py)."""

    async def append(
        self,
        type: SessionEventType,
        *,
        agent_id: str | None = None,
        subtask_id: str | None = None,
        invocation_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> object: ...


# Set by the engine for the lifetime of a run; inherited by every task it spawns.
_current_session: ContextVar[SessionWriter | None] = ContextVar("_current_session", default=None)


async def record(
    type: SessionEventType,
    *,
    agent_id: str | None = None,
    subtask_id: str | None = None,
    invocation_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    """Append to the active run's session log; a no-op outside a run."""
    session = _current_session.get()
    if session is not None:
        await session.append(
            type, agent_id=agent_id, subtask_id=subtask_id, invocation_id=invocation_id, data=data
        )


def result_data(result: AgentResult) -> dict[str, Any]:
    """AgentResult as logged: the message history is already in the invocation's
    message events, so repeating it here would double the log size."""
    return {"result": result.model_dump(mode="json", exclude={"messages"})}


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

@dataclass
class Invocation:
    """One Agent.run call, as reconstructed from the log."""
    invocation_id: str
    agent_id: str | None
    task_id: str | None
    messages: list[dict[str, Any]] = field(default_factory=list)
    finished: bool = False
    status: str | None = None
    # Sum of the per-call cost_usd on this invocation's live assistant turns (the
    # inherited prefix a resumed invocation re-logs carries none).
    cost_usd: float = 0.0


@dataclass
class RunSnapshot:
    run_id: str
    task: str = ""
    user_context: dict[str, Any] = field(default_factory=dict)
    budget_usd: float | None = None
    workspace: str | None = None
    plan: ExecutionPlan | None = None
    dispatched: dict[str, str] = field(default_factory=dict)  # subtask_id → task_id
    finished: dict[str, bool] = field(default_factory=dict)    # subtask_id → ok
    micro_plans: dict[str, list[Subtask]] = field(default_factory=dict)  # parent → micro
    results: dict[str, AgentResult] = field(default_factory=dict)
    total_cost_usd: float = 0.0
    invocations: dict[str, Invocation] = field(default_factory=dict)
    status: str | None = None  # run_finished status; None while in flight
    interrupted: bool = False  # run_interrupted seen and not yet resumed
    last_seq: int = 0

    @property
    def unfinished_invocations(self) -> list[Invocation]:
        return [inv for inv in self.invocations.values() if not inv.finished]

    @property
    def resumable(self) -> bool:
        # "interrupted" as a terminal status was written by reconciliation before
        # run_interrupted existed; such runs are resumable too.
        return self.status is None or self.status == "interrupted"


def replay(run_id: str, events: list[SessionEvent]) -> RunSnapshot:
    """Fold a session log into the run state it describes."""
    snap = RunSnapshot(run_id=run_id)
    orphaned: set[str] = set()  # invocations cut off by an interruption, already costed
    for e in events:
        snap.last_seq = e.seq
        d = e.data
        match e.type:
            case SessionEventType.run_started:
                snap.task = d.get("task", "")
                snap.user_context = d.get("user_context") or {}
                snap.budget_usd = d.get("budget_usd")
                snap.workspace = d.get("workspace")
            case SessionEventType.plan_created:
                snap.plan = ExecutionPlan.model_validate(d["plan"])
            case SessionEventType.subtask_decomposed:
                if e.subtask_id is not None:
                    snap.micro_plans[e.subtask_id] = [Subtask.model_validate(m) for m in d["micro"]]
            case SessionEventType.subtask_finished:
                if e.subtask_id is not None:
                    snap.finished[e.subtask_id] = bool(d.get("ok"))
            case SessionEventType.subtask_dispatched:
                if e.subtask_id is not None:
                    snap.dispatched[e.subtask_id] = d.get("task_id", "")
            case SessionEventType.result_stored:
                if e.subtask_id is not None:
                    snap.results[e.subtask_id] = AgentResult.model_validate(d["result"])
            case SessionEventType.cost_added:
                snap.total_cost_usd += d.get("cost_usd", 0.0)
            case SessionEventType.human_input_received:
                if d.get("budget_usd") is not None:
                    snap.budget_usd = d["budget_usd"]
            case SessionEventType.agent_started:
                if e.invocation_id is not None:
                    snap.invocations[e.invocation_id] = Invocation(
                        invocation_id=e.invocation_id, agent_id=e.agent_id, task_id=d.get("task_id")
                    )
            case SessionEventType.message_appended:
                inv = snap.invocations.get(e.invocation_id or "")
                if inv is not None:
                    inv.messages.append(d["message"])
                    inv.cost_usd += d.get("cost_usd", 0.0)
            case SessionEventType.message_discarded:
                inv = snap.invocations.get(e.invocation_id or "")
                if inv is not None and inv.messages:
                    inv.messages.pop()
            case SessionEventType.agent_finished:
                inv = snap.invocations.get(e.invocation_id or "")
                if inv is not None:
                    inv.finished = True
                    inv.status = d.get("status")
            case SessionEventType.run_interrupted:
                snap.interrupted = True
                # An invocation open at an interruption never finishes (a resume
                # continues its transcript in a new invocation), so its calls never
                # reach cost_added. Count what it spent here instead.
                for inv in snap.invocations.values():
                    if not inv.finished and inv.invocation_id not in orphaned:
                        orphaned.add(inv.invocation_id)
                        snap.total_cost_usd += inv.cost_usd
            case SessionEventType.run_resumed:
                snap.interrupted = False
                snap.status = None
            case SessionEventType.run_finished:
                snap.status = d.get("status")
    return snap
