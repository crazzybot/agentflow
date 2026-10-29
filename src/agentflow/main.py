"""AgentFlow — FastAPI application entry point."""
from __future__ import annotations

import os
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agentflow.config import settings
from agentflow.core.registry import AgentRegistry
from agentflow.logging_config import setup_logging
from agentflow.orchestrator.engine import OrchestratorEngine
from agentflow.orchestrator.resume import RunNotResumableError

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


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    reconciled = engine.reconcile_orphaned_runs()
    if reconciled:
        logger.warning("Reconciled %d orphaned run(s) from previous instance", len(reconciled))
    if settings.resume_interrupted_runs:
        for run_id in reconciled:
            try:
                engine.schedule_resume(run_id)
                logger.info("Resuming interrupted run %s", run_id)
            except RunNotResumableError as exc:
                # Runs recorded before session logs existed cannot be resumed.
                logger.info("Not resuming %s: %s", run_id, exc)
    yield
    if settings.state_backend == "redis":
        from agentflow.core.redis_client import close_redis
        await close_redis()
        logger.info("Redis connection pool closed")


app = FastAPI(
    title="AgentFlow",
    version="0.1.0",
    description="Multi-agent orchestration system",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000", 
        "http://localhost:5173", 
        "http://localhost:5174",
        "http://localhost:5175",  
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from agentflow.api.routes import router  # noqa: E402
app.include_router(router, prefix="/api")


@app.get("/health")
async def health() -> dict:
    agents = [a.agent_id for a in registry.all()]
    return {"status": "ok", "agents": agents}
