"""AgentRegistry — loads manifests and provides capability-based lookup."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml

from agentflow.core.models import AgentManifest

logger = logging.getLogger(__name__)


def _load_manifest_file(path: Path) -> dict:
    text = path.read_text()
    if path.suffix in {".yaml", ".yml"}:
        return yaml.safe_load(text)
    return json.loads(text)


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, AgentManifest] = {}
        # capability → list of agent_ids
        self._capability_index: dict[str, list[str]] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, manifest: AgentManifest) -> None:
        self._agents[manifest.agent_id] = manifest
        for cap in manifest.capabilities:
            self._capability_index.setdefault(cap, []).append(manifest.agent_id)
        logger.info("Registered agent %s with capabilities %s", manifest.agent_id, manifest.capabilities)

    def load_from_directory(self, directory: str | Path) -> None:
        dir_path = Path(directory)
        if not dir_path.exists():
            logger.warning("Manifests directory %s does not exist — skipping", dir_path)
            return
        patterns = ("*.json", "*.yaml", "*.yml")
        seen: set[str] = set()
        for pattern in patterns:
            for manifest_file in sorted(dir_path.glob(pattern)):
                if manifest_file.stem in seen:
                    logger.warning("Skipping duplicate manifest %s (already loaded)", manifest_file)
                    continue
                try:
                    data = _load_manifest_file(manifest_file)
                    manifest = AgentManifest(**data)
                    self.register(manifest)
                    seen.add(manifest_file.stem)
                except Exception as exc:
                    logger.error("Failed to load manifest %s: %s", manifest_file, exc)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, agent_id: str) -> AgentManifest | None:
        return self._agents.get(agent_id)

    def all(self) -> list[AgentManifest]:
        return list(self._agents.values())

    def by_capability(self, capability: str) -> list[AgentManifest]:
        ids = self._capability_index.get(capability, [])
        return [self._agents[i] for i in ids if i in self._agents]

    def find_fallback(self, for_agent_id: str) -> AgentManifest | None:
        """Return an agent declared as fallback for *for_agent_id*."""
        for manifest in self._agents.values():
            if for_agent_id in manifest.fallback_for:
                return manifest
        return None

    # ------------------------------------------------------------------
    # Summary for LLM planner prompt
    # ------------------------------------------------------------------

    def summary(self) -> str:
        """Return a structured agent roster for the LLM planner.

        Each entry surfaces the fields the planner needs to make routing and
        skill-loading decisions without any hardcoded agent names in the prompt.
        """
        if not self._agents:
            return "(no agents registered)"
        blocks = []
        for agent in self._agents.values():
            parts = [f"## {agent.agent_id}"]
            parts.append(f"  domain: {agent.domain}")
            parts.append(f"  capabilities: {', '.join(agent.capabilities) or 'none'}")
            parts.append(f"  tools: {', '.join(agent.tools) or 'none'}")
            if agent.skills:
                parts.append(f"  skills: {', '.join(agent.skills)}")
            blocks.append("\n".join(parts))
        return "\n\n".join(blocks)
