"""Tests for AgentRegistry."""
import pytest
from agentflow.core.models import AgentManifest
from agentflow.core.registry import AgentRegistry


def _make_manifest(agent_id: str, capabilities: list[str], fallback_for: list[str] | None = None) -> AgentManifest:
    return AgentManifest(
        agent_id=agent_id,
        domain="Test",
        capabilities=capabilities,
        system_prompt="test",
        fallback_for=fallback_for or [],
    )


def test_register_and_get():
    registry = AgentRegistry()
    m = _make_manifest("AgentA", ["cap1"])
    registry.register(m)
    assert registry.get("AgentA") == m
    assert registry.get("AgentB") is None


def test_by_capability():
    registry = AgentRegistry()
    registry.register(_make_manifest("AgentA", ["research", "synthesis"]))
    registry.register(_make_manifest("AgentB", ["coding"]))
    results = registry.by_capability("research")
    assert len(results) == 1
    assert results[0].agent_id == "AgentA"


def test_find_fallback():
    registry = AgentRegistry()
    registry.register(_make_manifest("AgentA", []))
    registry.register(_make_manifest("AgentB", [], fallback_for=["AgentA"]))
    fallback = registry.find_fallback("AgentA")
    assert fallback is not None
    assert fallback.agent_id == "AgentB"


def test_summary_non_empty():
    registry = AgentRegistry()
    registry.register(_make_manifest("AgentA", ["cap1"]))
    summary = registry.summary()
    assert "AgentA" in summary
    assert "cap1" in summary


def test_load_from_directory(tmp_path):
    import json
    manifest_data = {
        "agent_id": "TestAgent",
        "domain": "Test",
        "capabilities": ["testing"],
        "system_prompt": "test",
    }
    (tmp_path / "test_agent.json").write_text(json.dumps(manifest_data))
    registry = AgentRegistry()
    registry.load_from_directory(tmp_path)
    assert registry.get("TestAgent") is not None
