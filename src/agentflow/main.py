"""AgentFlow — FastAPI application entry point."""
from __future__ import annotations

import os
import logging

from fastapi import FastAPI

from agentflow.config import settings
from agentflow.core.registry import AgentRegistry
from agentflow.logging_config import setup_logging
from agentflow.orchestrator.engine import OrchestratorEngine

setup_logging(
    level=os.getenv("LOG_LEVEL", "INFO"), 
    json_format=os.getenv("LOG_JSON", "false").lower() == "true",
    log_file=os.getenv("LOG_FILE", None)
)
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
