"""FastAPI routes — POST /run and GET /run/:id/stream."""
from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException
from sse_starlette.sse import EventSourceResponse

from agentflow.core.models import RunRequest, RunResponse
from agentflow.orchestrator.stream import stream_registry

router = APIRouter()


def _get_engine():
    from agentflow.main import engine
    return engine


@router.post("/run", response_model=RunResponse)
async def start_run(request: RunRequest, background_tasks: BackgroundTasks):
    run_id = str(uuid.uuid4())
    engine = _get_engine()

    # Run orchestration in the background so we can return the run_id immediately
    background_tasks.add_task(engine.run, run_id, request.task, request.context)

    # Wait briefly for the emitter to be created before client can connect
    for _ in range(20):
        if stream_registry.get(run_id):
            break
        await asyncio.sleep(0.05)

    return RunResponse(run_id=run_id)


@router.get("/run/{run_id}/stream")
async def stream_run(run_id: str):
    emitter = stream_registry.get(run_id)
    if emitter is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")
    return EventSourceResponse(emitter)
