"""FastAPI routes — POST /run, GET /run/:id/stream, and past-run query endpoints."""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException
from sse_starlette.sse import EventSourceResponse

from agentflow.config import settings
from agentflow.core.models import (
    RunEventsResponse,
    RunInfo,
    RunListResponse,
    RunMeta,
    RunReportResponse,
    RunRequest,
    RunResponse,
    RunResultsResponse,
    SSEEvent,
    SubtaskResult,
)
from agentflow.orchestrator.stream import stream_registry

router = APIRouter()


def _get_engine():
    from agentflow.main import engine
    return engine


@router.post("/runs", response_model=RunResponse)
async def start_run(request: RunRequest, background_tasks: BackgroundTasks):
    run_id = str(uuid.uuid4())
    engine = _get_engine()

    # Run orchestration in the background so we can return the run_id immediately
    background_tasks.add_task(engine.run, run_id, request.task, request.context, request.budget_usd)

    # Wait briefly for the emitter to be created before client can connect
    for _ in range(20):
        if stream_registry.get(run_id):
            break
        await asyncio.sleep(0.05)

    return RunResponse(run_id=run_id)


@router.get("/runs/{run_id}/stream")
async def stream_run(run_id: str):
    emitter = stream_registry.get(run_id)
    if emitter is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")
    return EventSourceResponse(emitter)


# ---------------------------------------------------------------------------
# Past-run query endpoints
# ---------------------------------------------------------------------------


def _run_dir(run_id: str) -> Path:
    return Path(settings.runs_dir) / run_id


def _require_run(run_id: str) -> Path:
    d = _run_dir(run_id)
    if not d.is_dir():
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")
    return d


def _load_meta(d: Path) -> RunMeta | None:
    meta_file = d / "meta.json"
    if not meta_file.exists():
        return None
    try:
        return RunMeta.model_validate_json(meta_file.read_text())
    except Exception:
        return None


def _run_info(d: Path) -> RunInfo:
    meta = _load_meta(d)
    return RunInfo(
        run_id=d.name,
        has_events=(d / "events.jsonl").exists(),
        has_results=(d / "results.jsonl").exists(),
        has_report=(d / "report.md").exists(),
        task=meta.task if meta else None,
        name=meta.name if meta else None,
        created_at=meta.created_at if meta else None,
    )


@router.get("/runs", response_model=RunListResponse)
async def list_runs():
    runs_dir = Path(settings.runs_dir)
    if not runs_dir.exists():
        return RunListResponse(runs=[])
    runs = [_run_info(d) for d in runs_dir.iterdir() if d.is_dir()]
    runs.sort(key=lambda r: r.created_at or "", reverse=True)
    return RunListResponse(runs=runs)


@router.get("/runs/{run_id}", response_model=RunInfo)
async def get_run(run_id: str):
    d = _require_run(run_id)
    return _run_info(d)


@router.get("/runs/{run_id}/events", response_model=RunEventsResponse)
async def get_run_events(run_id: str):
    d = _require_run(run_id)
    events_file = d / "events.jsonl"
    if not events_file.exists():
        raise HTTPException(status_code=404, detail="No events captured for this run")
    events = [
        SSEEvent.model_validate(json.loads(line))
        for line in events_file.read_text().splitlines()
        if line.strip()
    ]
    return RunEventsResponse(run_id=run_id, events=events)


@router.get("/runs/{run_id}/results", response_model=RunResultsResponse)
async def get_run_results(run_id: str):
    d = _require_run(run_id)
    results_file = d / "results.jsonl"
    if not results_file.exists():
        raise HTTPException(status_code=404, detail="No results captured for this run")
    results = [
        SubtaskResult.model_validate(json.loads(line))
        for line in results_file.read_text().splitlines()
        if line.strip()
    ]
    return RunResultsResponse(run_id=run_id, results=results)


@router.get("/runs/{run_id}/report", response_model=RunReportResponse)
async def get_run_report(run_id: str):
    d = _require_run(run_id)
    report_file = d / "report.md"
    if not report_file.exists():
        raise HTTPException(status_code=404, detail="No report for this run")
    return RunReportResponse(run_id=run_id, report=report_file.read_text(encoding="utf-8"))
