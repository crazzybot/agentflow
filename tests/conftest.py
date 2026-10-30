"""Suite-wide fixtures."""
import pytest

from agentflow.core.lease import RunLeases


@pytest.fixture(autouse=True)
def _in_process_run_leases(monkeypatch):
    """The engine awaits its lease calls, so a developer .env with STATE_BACKEND=redis
    and no Redis server would fail every engine test. Tests that exercise Redis
    leases build RedisRunLeases on fakeredis explicitly."""
    monkeypatch.setattr("agentflow.orchestrator.engine.run_leases", RunLeases())
    monkeypatch.setattr("agentflow.core.lease.run_leases", RunLeases())  # campaigns


@pytest.fixture(autouse=True)
def _isolated_data_dirs(tmp_path_factory, monkeypatch):
    """The default WORKSPACE_DIR and RUNS_DIR are under the user's home
    (AGENTFLOW_HOME); a test that resolves either without patching it must never
    create or write files there. Tests that need a specific dir override this
    with their own monkeypatch."""
    home = tmp_path_factory.mktemp("agentflow-home")
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(home / "workspace"))
    monkeypatch.setattr("agentflow.config.settings.runs_dir", str(home / "runs"))
