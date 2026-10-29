"""Suite-wide fixtures."""
import pytest

from agentflow.core.lease import RunLeases


@pytest.fixture(autouse=True)
def _in_process_run_leases(monkeypatch):
    """The engine awaits its lease calls, so a developer .env with STATE_BACKEND=redis
    and no Redis server would fail every engine test. Tests that exercise Redis
    leases build RedisRunLeases on fakeredis explicitly."""
    monkeypatch.setattr("agentflow.orchestrator.engine.run_leases", RunLeases())
