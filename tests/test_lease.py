"""Tests for per-run ownership leases (Redis via fakeredis with Lua)."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from redis.exceptions import ConnectionError as RedisConnectionError

from agentflow.core.bus import TaskBus
from agentflow.core.context import ContextStore
from agentflow.core.lease import LEASE_LOST, RedisRunLeases
from agentflow.core.registry import AgentRegistry
from agentflow.core.session import RunSession, SessionEventType, read_session, replay
from agentflow.orchestrator.engine import OrchestratorEngine, RunAlreadyActiveError
from agentflow.orchestrator.stream import StreamRegistry

T = SessionEventType


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


def _leases(server: FakeServer, owner: str, ttl_s: float = 0.3) -> RedisRunLeases:
    redis = FakeAsyncRedis(server=server, decode_responses=True)
    return RedisRunLeases(redis, ttl_s=ttl_s, cancel_flag_ttl_s=60, owner_id=owner)


# ---------------------------------------------------------------------------
# Lease semantics
# ---------------------------------------------------------------------------

async def test_acquire_is_exclusive_and_reentrant(server):
    a, b = _leases(server, "A"), _leases(server, "B")

    assert await a.acquire("r")
    assert await a.acquire("r")  # same owner
    assert not await b.acquire("r")
    assert await b.held_elsewhere("r") and not await a.held_elsewhere("r")


async def test_release_only_drops_own_lease(server):
    a, b = _leases(server, "A"), _leases(server, "B")
    await a.acquire("r")

    await b.release("r")
    assert await b.held_elsewhere("r")

    await a.release("r")
    assert not await b.held_elsewhere("r")
    assert await b.acquire("r")


async def test_lease_lapses_without_renewal(server):
    a, b = _leases(server, "A", ttl_s=0.05), _leases(server, "B")
    await a.acquire("r")
    await asyncio.sleep(0.1)
    assert await b.acquire("r")  # owner presumed dead


async def test_keep_alive_renews_past_the_ttl(server):
    a, b = _leases(server, "A", ttl_s=0.15), _leases(server, "B")
    await a.acquire("r")
    run = asyncio.create_task(asyncio.Event().wait())
    hb = asyncio.create_task(a.keep_alive("r", run))
    await asyncio.sleep(0.4)  # well past one TTL

    assert await b.held_elsewhere("r")
    assert not run.cancelled()
    hb.cancel()
    run.cancel()


async def test_cancel_request_reaches_the_owner(server):
    a, b = _leases(server, "A"), _leases(server, "B")
    assert not await b.request_cancel("r")  # nobody owns it

    await a.acquire("r")
    run = asyncio.create_task(asyncio.Event().wait())
    hb = asyncio.create_task(a.keep_alive("r", run))
    assert await b.request_cancel("r")
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, timeout=1)
    await hb


async def test_fresh_owner_ignores_a_stale_cancel_flag(server):
    a, b = _leases(server, "A", ttl_s=0.05), _leases(server, "B")
    await a.acquire("r")
    await b.request_cancel("r")
    await asyncio.sleep(0.1)  # A died with the flag set

    assert await b.acquire("r")
    run = asyncio.create_task(asyncio.Event().wait())
    hb = asyncio.create_task(b.keep_alive("r", run))
    await asyncio.sleep(0.15)
    assert not run.done()
    hb.cancel()
    run.cancel()


async def test_lost_lease_cancels_with_marker(server):
    a = _leases(server, "A")
    await a.acquire("r")
    run = asyncio.create_task(asyncio.Event().wait())
    hb = asyncio.create_task(a.keep_alive("r", run))
    await FakeAsyncRedis(server=server, decode_responses=True).set("run:r:lease", "B")

    with pytest.raises(asyncio.CancelledError) as exc:
        await asyncio.wait_for(run, timeout=1)
    assert LEASE_LOST in exc.value.args
    await hb


async def test_transient_redis_error_does_not_kill_the_run(server):
    a = _leases(server, "A", ttl_s=0.15)
    await a.acquire("r")
    real_eval = a._redis.eval
    a._redis.eval = AsyncMock(side_effect=RedisConnectionError("blip"))
    run = asyncio.create_task(asyncio.Event().wait())
    hb = asyncio.create_task(a.keep_alive("r", run))
    await asyncio.sleep(0.08)  # one failed renew
    a._redis.eval = real_eval
    await asyncio.sleep(0.1)

    assert not run.done()
    hb.cancel()
    run.cancel()


# ---------------------------------------------------------------------------
# Engine across two replicas sharing one Redis
# ---------------------------------------------------------------------------

@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    d = tmp_path / "runs"
    monkeypatch.setattr("agentflow.config.settings.runs_dir", str(d))
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path / "workspace"))
    monkeypatch.setattr("agentflow.config.settings.capture_events", False)
    monkeypatch.setattr("agentflow.config.settings.capture_results", False)
    monkeypatch.setattr("agentflow.config.settings.direct_agent_id", "")
    monkeypatch.setattr("agentflow.orchestrator.engine.stream_registry", StreamRegistry())
    monkeypatch.setattr("agentflow.orchestrator.engine.context_store", ContextStore())
    monkeypatch.setattr("agentflow.orchestrator.engine.task_bus", TaskBus())
    return d


@pytest.fixture
def use_leases(monkeypatch):
    """Point the engine module at one replica's lease client. Each OrchestratorEngine
    plays a separate API process; switching the binding switches which "process"
    the next engine call runs as."""
    def bind(leases: RedisRunLeases) -> None:
        monkeypatch.setattr("agentflow.orchestrator.engine.run_leases", leases)
    return bind


async def _start_hanging_run(eng: OrchestratorEngine, run_id: str) -> asyncio.Task[None]:
    planning = asyncio.Event()

    async def hang(*_a, **_k):
        planning.set()
        await asyncio.Event().wait()

    patcher = patch("agentflow.orchestrator.engine.create_plan", AsyncMock(side_effect=hang))
    patcher.start()
    task = asyncio.create_task(eng.run(run_id, "t", {}))
    await planning.wait()
    task.add_done_callback(lambda _t: patcher.stop())
    return task


async def test_cancel_from_another_replica(server, runs_dir, use_leases):
    a_leases, b_leases = _leases(server, "A", ttl_s=0.15), _leases(server, "B", ttl_s=0.15)
    a = OrchestratorEngine(AgentRegistry())
    a._generate_run_name = AsyncMock(return_value="n")
    use_leases(a_leases)
    run = await _start_hanging_run(a, "r")

    b = OrchestratorEngine(AgentRegistry())
    use_leases(b_leases)
    assert await b.request_cancel("r") == "cancel_requested"

    use_leases(a_leases)  # A's own cleanup (release) must use A's owner id
    await asyncio.wait_for(run, timeout=2)
    assert replay("r", read_session("r")).status == "cancelled"
    assert not await b_leases.held_elsewhere("r")  # released on exit


async def test_unknown_run_cancel_returns_none(server, runs_dir, use_leases):
    use_leases(_leases(server, "B"))
    assert await OrchestratorEngine(AgentRegistry()).request_cancel("nope") is None


async def test_lost_lease_stands_down_without_a_terminal_event(server, runs_dir, use_leases):
    a_leases = _leases(server, "A", ttl_s=0.15)
    a = OrchestratorEngine(AgentRegistry())
    a._generate_run_name = AsyncMock(return_value="n")
    use_leases(a_leases)
    run = await _start_hanging_run(a, "r")

    # Another replica took the run over (A's loop stalled past the TTL).
    await FakeAsyncRedis(server=server, decode_responses=True).set("run:r:lease", "B")
    await asyncio.wait_for(run, timeout=2)

    snap = replay("r", read_session("r"))
    assert snap.status is None  # no run_finished written into B's log
    from agentflow.orchestrator import engine as engine_mod
    events = engine_mod.stream_registry.get("r")._buffer
    assert not any(e.type == "run:cancelled" for e in events)
    assert await FakeAsyncRedis(server=server, decode_responses=True).get("run:r:lease") == "B"


async def test_reconcile_and_resume_skip_runs_live_on_another_replica(server, runs_dir, use_leases):
    (runs_dir / "r").mkdir(parents=True)
    (runs_dir / "r" / "meta.json").write_text("{}")
    await RunSession("r").append(T.run_started, data={"task": "t", "workspace": "runs/r"})
    await _leases(server, "A", ttl_s=30).acquire("r")  # alive on replica A

    use_leases(_leases(server, "B"))
    b = OrchestratorEngine(AgentRegistry())
    assert await b.reconcile_orphaned_runs() == []
    with pytest.raises(RunAlreadyActiveError, match="another replica"):
        await b.schedule_resume("r")
    assert not (runs_dir / "r" / "report.md").exists()
