"""AgentRegistry — loads manifests and provides capability-based lookup."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from agentflow.core.models import AgentManifest

logger = logging.getLogger(__name__)


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
        for manifest_file in dir_path.glob("*.json"):
            try:
                data = json.loads(manifest_file.read_text())
                manifest = AgentManifest(**data)
                self.register(manifest)
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
        lines = []
        for agent in self._agents.values():
            lines.append(
                f"- {agent.agent_id} (domain: {agent.domain}): capabilities={agent.capabilities}, tools={agent.tools}"
            )
        return "\n".join(lines) if lines else "(no agents registered)"
