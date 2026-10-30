"""Running subtask agents in separate worker processes ("many hands").

With ``DISPATCH_MODE=workers`` the engine keeps everything that decides —
planning, scheduling, decomposition, verification, checkpoints, the session log
and leases — and hands each *subtask agent invocation* to a worker through
Redis. Workers can run on other machines, with their own sandboxes; a worker
started with ``--agents`` only serves those agents.

Redis keys (all with ``redis_key_ttl``):

  agentflow:work:<agent_id>   List — queued WorkItems (RPUSH by engine, BLPOP by workers)
  work:<item>:claimed         String — worker id, TTL refreshed by the worker's heartbeat
  work:<item>:events          List — session events relayed from the worker
  work:<item>:result          List — the final AgentResult (one entry)
  work:<item>:cancel          String — set by the engine to stop the invocation

Failure handling is folded into the existing retry/fallback logic: an item no
worker claims within ``worker_claim_timeout_s``, or whose worker stops
heartbeating, comes back as a *failed* AgentResult. Cancelling the waiting
engine task (run cancel, task timeout) sets the cancel flag and the worker stops.

The session log keeps a single writer: workers never touch ``session.jsonl``;
their ``record()`` calls go to ``RemoteSessionWriter`` and the engine appends
them with its own sequence numbers as it drains them.

Requirements: ``STATE_BACKEND=redis`` (SSE, user messages and HITL already work
across processes), and ``runs_dir`` + the workspace root shared with workers
(artifacts, workspace files). Not covered: a worker invocation's own KB ingest
dispatch, and events still in Redis when the engine owning the run dies.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import redis.asyncio as aioredis

from agentflow.agents.agent import Agent
from agentflow.config import settings
from agentflow.core.models import AgentResult, AgentStatus, TaskEnvelope, WorkItem
from agentflow.core.registry import AgentRegistry
from agentflow.core.session import SessionEventType, _current_session, record
from agentflow.core.workspace import _current_workspace, current_workspace, workspace_path, workspace_root
from agentflow.tools.artifact_tracker import ArtifactSink, _current_sink
from agentflow.tools.sandbox import SandboxProvider, _current_sandbox

if TYPE_CHECKING:
    from agentflow.llm import LLMClient

logger = logging.getLogger(__name__)


def queue_key(agent_id: str) -> str:
    return f"agentflow:work:{agent_id}"


def _item_key(item_id: str, part: str) -> str:
    return f"work:{item_id}:{part}"


def _failed(envelope: TaskEnvelope, agent_id: str, error: str) -> AgentResult:
    return AgentResult(task_id=envelope.task_id, agent_id=agent_id, status=AgentStatus.failed, error=error)


# ---------------------------------------------------------------------------
# Engine side
# ---------------------------------------------------------------------------

class RemoteInvoker:
    """Queues an agent invocation for a worker and waits for its result."""

    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis

    async def invoke(
        self,
        run_id: str,
        agent_id: str,
        envelope: TaskEnvelope,
        resume_messages: list[dict[str, Any]] | None = None,
    ) -> AgentResult:
        item = WorkItem(
            item_id=uuid.uuid4().hex,
            run_id=run_id,
            agent_id=agent_id,
            envelope=envelope,
            resume_messages=resume_messages,
            workspace=current_workspace().relative_to(workspace_root()).as_posix() or ".",
        )
        payload = item.model_dump_json()
        events_key = _item_key(item.item_id, "events")
        result_key = _item_key(item.item_id, "result")
        claimed_key = _item_key(item.item_id, "claimed")
        await self._redis.rpush(queue_key(agent_id), payload)
        claim_deadline = time.monotonic() + settings.worker_claim_timeout_s
        was_claimed = False
        try:
            while True:
                # Events before the result: a worker pushes its whole transcript first.
                popped = await self._redis.blpop([events_key, result_key], timeout=1)
                if popped is not None:
                    key, data = popped
                    if key == events_key:
                        await _append_relayed(json.loads(data))
                        continue
                    return AgentResult.model_validate_json(data)
                if await self._redis.exists(claimed_key):
                    was_claimed = True
                elif was_claimed:
                    # Claimed, then the heartbeat lapsed without a result.
                    return _failed(envelope, agent_id, "worker lost (stopped heartbeating)")
                elif time.monotonic() > claim_deadline:
                    if await self._redis.lrem(queue_key(agent_id), 1, payload):
                        return _failed(
                            envelope, agent_id,
                            f"no worker claimed the task within {settings.worker_claim_timeout_s:g}s "
                            f"(is a worker serving {agent_id!r} running?)",
                        )
                    was_claimed = True  # taken between our checks; its claim key is due
        except asyncio.CancelledError:
            await self._redis.set(_item_key(item.item_id, "cancel"), "1", ex=settings.redis_key_ttl)
            await self._redis.lrem(queue_key(agent_id), 1, payload)  # if still queued
            raise
        finally:
            await self._redis.delete(events_key, result_key)


async def _append_relayed(event: dict[str, Any]) -> None:
    await record(
        SessionEventType(event["type"]),
        agent_id=event.get("agent_id"),
        subtask_id=event.get("subtask_id"),
        invocation_id=event.get("invocation_id"),
        data=event.get("data") or {},
    )


# ---------------------------------------------------------------------------
# Worker side
# ---------------------------------------------------------------------------

class RemoteSessionWriter:
    """A worker's record() target: relays session events to the engine owning the run."""

    def __init__(self, redis: aioredis.Redis, item_id: str) -> None:
        self._redis = redis
        self._key = _item_key(item_id, "events")

    async def append(
        self,
        type: SessionEventType,
        *,
        agent_id: str | None = None,
        subtask_id: str | None = None,
        invocation_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        event = {"type": type.value, "agent_id": agent_id, "subtask_id": subtask_id,
                 "invocation_id": invocation_id, "data": data or {}}
        await self._redis.rpush(self._key, json.dumps(event))
        await self._redis.expire(self._key, settings.redis_key_ttl)


class Worker:
    """Pulls WorkItems for the agents it serves and runs them, ``concurrency`` at a time."""

    def __init__(
        self,
        registry: AgentRegistry,
        client: LLMClient,
        redis: aioredis.Redis,
        agent_ids: list[str] | None = None,
        concurrency: int = 1,
    ) -> None:
        self.registry = registry
        self.client = client
        self.redis = redis
        self.agent_ids = agent_ids or [m.agent_id for m in registry.all()]
        unknown = [a for a in self.agent_ids if registry.get(a) is None]
        if unknown:
            raise ValueError(f"Worker cannot serve unknown agent(s): {unknown}")
        self.concurrency = concurrency
        self.worker_id = f"worker-{uuid.uuid4().hex[:8]}"

    async def run_forever(self) -> None:
        logger.info("%s serving %s (concurrency %d)", self.worker_id, self.agent_ids, self.concurrency)
        await asyncio.gather(*(self._loop() for _ in range(self.concurrency)))

    async def _loop(self) -> None:
        keys = [queue_key(a) for a in self.agent_ids]
        while True:
            popped = await self.redis.blpop(keys, timeout=5)
            if popped is None:
                continue
            _, data = popped
            try:
                item = WorkItem.model_validate_json(data)
            except ValueError:
                logger.exception("%s dropping malformed work item", self.worker_id)
                continue
            await self.handle(item)

    async def handle(self, item: WorkItem) -> None:
        claimed_key = _item_key(item.item_id, "claimed")
        ttl = max(1, int(settings.worker_heartbeat_s * 3))
        await self.redis.set(claimed_key, self.worker_id, ex=ttl)
        task = asyncio.create_task(self._execute(item))
        beat = asyncio.create_task(self._heartbeat(item, task, ttl))
        try:
            result = await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                task.cancel()  # the worker itself is shutting down: stop the agent too
                raise
            logger.info("%s cancelled %s on request", self.worker_id, item.item_id)
            return
        finally:
            beat.cancel()
            await self.redis.delete(claimed_key)
        await self.redis.rpush(_item_key(item.item_id, "result"), result.model_dump_json())
        await self.redis.expire(_item_key(item.item_id, "result"), settings.redis_key_ttl)

    async def _heartbeat(self, item: WorkItem, task: asyncio.Task[AgentResult], ttl: int) -> None:
        while True:
            await asyncio.sleep(settings.worker_heartbeat_s)
            await self.redis.set(_item_key(item.item_id, "claimed"), self.worker_id, ex=ttl)
            if await self.redis.exists(_item_key(item.item_id, "cancel")):
                task.cancel()
                return

    async def _execute(self, item: WorkItem) -> AgentResult:
        from agentflow.core.context import context_store
        from agentflow.orchestrator.stream import StreamEmitter, stream_registry

        manifest = self.registry.get(item.agent_id)
        if manifest is None:
            return _failed(item.envelope, item.agent_id, f"worker does not know agent {item.agent_id!r}")
        ws = workspace_path(item.workspace)
        provider = SandboxProvider(ws, item.run_id)
        tokens = [
            (_current_workspace, _current_workspace.set(ws)),
            (_current_session, _current_session.set(RemoteSessionWriter(self.redis, item.item_id))),
            (_current_sandbox, _current_sandbox.set(provider)),
            (_current_sink, _current_sink.set(
                ArtifactSink(Path(settings.runs_dir) / item.run_id / "artifacts.jsonl"))),
        ]
        try:
            # Publishes to the run's SSE stream (Redis Streams: visible to every replica).
            emitter = await stream_registry.connect(item.run_id) or StreamEmitter(item.run_id)
            ctx = await context_store.connect(item.run_id)
            return await Agent(manifest, self.client).run(
                item.envelope, emitter, resume_messages=item.resume_messages, ctx=ctx,
            )
        finally:
            await provider.close()
            for var, token in reversed(tokens):
                var.reset(token)
