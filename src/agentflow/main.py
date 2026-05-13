"""AgentFlow — FastAPI application entry point."""
from __future__ import annotations

import logging

from fastapi import FastAPI

from agentflow.config import settings
from agentflow.core.registry import AgentRegistry
from agentflow.orchestrator.engine import OrchestratorEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global singletons
# ---------------------------------------------------------------------------

registry = AgentRegistry()
registry.load_from_directory(settings.manifests_dir)

engine = OrchestratorEngine(registry)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="AgentFlow", version="0.1.0", description="Multi-agent orchestration system")

from agentflow.api.routes import router  # noqa: E402
app.include_router(router, prefix="/api")


@app.get("/health")
async def health() -> dict:
    agents = [a.agent_id for a in registry.all()]
    return {"status": "ok", "agents": agents}
