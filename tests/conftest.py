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
def _isolated_workspace_root(tmp_path_factory, monkeypatch):
    """The default WORKSPACE_DIR is in the user's home; a test that resolves the
    workspace without patching it must never create or write files there.
    Tests that need a specific root override this with their own monkeypatch."""
    root = tmp_path_factory.mktemp("workspace-root")
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(root))
