"""Tests for running subtask agents on worker processes over Redis (fakeredis)."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from anthropic.types import TextBlock
from fakeredis import FakeAsyncRedis, FakeServer

from agentflow.core.bus import TaskBus
from agentflow.core.context_redis import RedisContextStore
from agentflow.core.models import (
    AgentManifest,
    AgentOutput,
    AgentResult,
    AgentStatus,
    ExecutionPlan,
    Subtask,
)
from agentflow.core.registry import AgentRegistry
from agentflow.core.session import SessionEventType, read_session, replay
from agentflow.orchestrator.engine import OrchestratorEngine
from agentflow.orchestrator.stream_redis import RedisStreamRegistry
from agentflow.orchestrator.workers import (
    RemoteInvoker,
    RemoteSessionWriter,
    Worker,
    queue_key,
)

T = SessionEventType


@pytest.fixture
def redis():
    return FakeAsyncRedis(server=FakeServer(), decode_responses=True)


@pytest.fixture
def env(tmp_path, monkeypatch, redis):
    monkeypatch.setattr("agentflow.config.settings.runs_dir", str(tmp_path / "runs"))
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path / "workspace"))
    for name, value in {
        "capture_events": False, "capture_results": False, "direct_agent_id": "",
        "enable_decomposer": False, "enable_checkpoints": False, "task_max_retries": 1,
        "worker_heartbeat_s": 0.05, "worker_claim_timeout_s": 2.0,
    }.items():
        monkeypatch.setattr(f"agentflow.config.settings.{name}", value)
    # Redis-backed state, as in a real multi-process deployment.
    streams, contexts = RedisStreamRegistry(redis), RedisContextStore(redis)
    for module in ("agentflow.orchestrator.engine", "agentflow.orchestrator.stream"):
        monkeypatch.setattr(f"{module}.stream_registry", streams)
    for module in ("agentflow.orchestrator.engine", "agentflow.core.context"):
        monkeypatch.setattr(f"{module}.context_store", contexts)
    monkeypatch.setattr("agentflow.orchestrator.engine.task_bus", TaskBus())


def _registry(*agent_ids: str) -> AgentRegistry:
    registry = AgentRegistry()
    for agent_id in agent_ids:
        registry.register(AgentManifest(agent_id=agent_id, domain="d", system_prompt="s"))
    return registry


def _engine(redis, *agent_ids: str) -> OrchestratorEngine:
    eng = OrchestratorEngine(_registry(*agent_ids))
    eng._generate_run_name = AsyncMock(return_value="n")
    eng._remote = RemoteInvoker(redis)  # DISPATCH_MODE=workers
    return eng


def _llm(text: str = '{"ok": true}') -> MagicMock:
    resp = MagicMock()
    resp.stop_reason, resp.content = "end_turn", [TextBlock(type="text", text=text)]
    resp.usage.input_tokens, resp.usage.output_tokens = 10, 5
    resp.usage.cache_creation_input_tokens = resp.usage.cache_read_input_tokens = 0
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=resp)
    return client


async def _run(eng: OrchestratorEngine, agent_id: str = "A") -> None:
    plan = ExecutionPlan(run_id="r", subtasks=[Subtask(id="s", agent_id=agent_id, instruction="do it")])
    with patch("agentflow.orchestrator.engine.compile_report", AsyncMock(return_value="report.md")):
        await eng.run("r", "t", {}, plan=plan)


async def _with_worker(worker: Worker, coro):
    task = asyncio.create_task(worker.run_forever())
    try:
        return await coro
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

async def test_subtask_runs_on_a_worker_and_its_transcript_reaches_the_log(env, redis):
    eng = _engine(redis, "A")
    worker_client = _llm()
    worker = Worker(_registry("A"), worker_client, redis)

    await _with_worker(worker, _run(eng))

    worker_client.messages.create.assert_awaited()  # the worker's LLM did the work
    events = read_session("r")
    snap = replay("r", events)
    assert snap.status == "complete"
    assert snap.results["s"].status == AgentStatus.success
    assert snap.results["s"].output.structured == {"ok": True}
    # The worker's invocation events were relayed and appended by the engine,
    # which stays the log's only writer: sequence numbers are unique and ordered.
    (inv,) = snap.invocations.values()
    assert inv.agent_id == "A" and inv.finished and len(inv.messages) == 2
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert await redis.llen(queue_key("A")) == 0


async def test_unclaimed_work_fails_the_attempt(env, redis, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.worker_claim_timeout_s", 0.5)
    eng = _engine(redis, "A", "B")
    worker = Worker(_registry("B"), _llm(), redis)  # serves B only

    await _with_worker(worker, _run(eng, "A"))

    result = replay("r", read_session("r")).results["s"]
    assert result.status == AgentStatus.failed and "no worker claimed" in result.error
    assert await redis.llen(queue_key("A")) == 0  # withdrawn, not left for a late worker


class _Hanging:
    started = asyncio.Event()
    cancelled = False

    def __init__(self, *_a):
        pass

    async def run(self, *_a, **_k):
        _Hanging.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            _Hanging.cancelled = True
            raise


@pytest.fixture
def hanging():
    _Hanging.started, _Hanging.cancelled = asyncio.Event(), False
    with patch("agentflow.orchestrator.workers.Agent", _Hanging):
        yield _Hanging


async def test_a_dead_worker_fails_the_attempt(env, redis, hanging):
    eng = _engine(redis, "A")
    worker = Worker(_registry("A"), _llm(), redis)
    worker_task = asyncio.create_task(worker.run_forever())
    run = asyncio.create_task(_run(eng))
    await asyncio.wait_for(hanging.started.wait(), 5)
    worker_task.cancel()  # the worker process dies mid-task
    await asyncio.gather(worker_task, return_exceptions=True)
    await asyncio.wait_for(run, 10)

    result = replay("r", read_session("r")).results["s"]
    assert result.status == AgentStatus.failed and "worker lost" in result.error


async def test_cancelling_the_run_stops_the_remote_agent(env, redis, hanging):
    eng = _engine(redis, "A")
    worker = Worker(_registry("A"), _llm(), redis)
    worker_task = asyncio.create_task(worker.run_forever())
    try:
        run = asyncio.create_task(_run(eng))
        await asyncio.wait_for(hanging.started.wait(), 5)
        assert eng.cancel_run("r")
        await run
        for _ in range(100):
            if hanging.cancelled:
                break
            await asyncio.sleep(0.02)
        assert hanging.cancelled
        assert not worker_task.done()  # the worker carries on serving
    finally:
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
    assert replay("r", read_session("r")).status == "cancelled"


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

async def test_remote_session_writer_relays_events(redis):
    writer = RemoteSessionWriter(redis, "item1")
    await writer.append(T.agent_started, agent_id="A", invocation_id="i", data={"task_id": "t"})
    (raw,) = await redis.lrange("work:item1:events", 0, -1)
    assert json.loads(raw) == {"type": "agent_started", "agent_id": "A", "subtask_id": None,
                               "invocation_id": "i", "data": {"task_id": "t"}}


def test_worker_refuses_unknown_agents(redis):
    with pytest.raises(ValueError, match="unknown agent"):
        Worker(_registry("A"), MagicMock(), redis, agent_ids=["Nope"])


def test_workers_mode_requires_redis(monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.dispatch_mode", "workers")
    monkeypatch.setattr("agentflow.config.settings.state_backend", "memory")
    with pytest.raises(ValueError, match="STATE_BACKEND=redis"):
        OrchestratorEngine(AgentRegistry())
    monkeypatch.setattr("agentflow.config.settings.dispatch_mode", "cluster")
    with pytest.raises(ValueError, match="Unknown DISPATCH_MODE"):
        OrchestratorEngine(AgentRegistry())


async def test_local_mode_runs_agents_in_process(env, redis):
    eng = _engine(redis, "A")
    eng._remote = None
    local = MagicMock()
    local.run = AsyncMock(return_value=AgentResult(task_id="t", agent_id="A", status=AgentStatus.success,
                                                   output=AgentOutput(text="here")))
    eng._agent_instances = {"A": local}
    await _run(eng)
    local.run.assert_awaited_once()
    assert await redis.llen(queue_key("A")) == 0
