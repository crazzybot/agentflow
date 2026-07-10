"""FastAPI routes — POST /run, GET /run/:id/stream, and past-run query endpoints."""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from agentflow.config import settings
from agentflow.core.context import context_store
from agentflow.core.models import (
    FollowUpRequest,
    HumanInputResponse,
    RunArtifact,
    RunArtifactContentResponse,
    RunArtifactsResponse,
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
    UserMessage,
)
from agentflow.orchestrator.stream import stream_registry

router = APIRouter()

# Cap concurrent Redis lookups in list_runs to avoid exhausting the connection pool.
_LIST_RUNS_SEM = asyncio.Semaphore(10)


def _get_engine():
    from agentflow.main import engine
    return engine


@router.post("/runs", response_model=RunResponse)
async def start_run(request: RunRequest):
    run_id = str(uuid.uuid4())
    engine = _get_engine()

    # asyncio.create_task schedules the coroutine on the running event loop
    # immediately, so it can start during the await-sleep poll below.
    # BackgroundTasks would only start after the response is sent, making the
    # poll a no-op and leaving a window where the SSE stream key doesn't exist.
    asyncio.create_task(
        engine.run(run_id, request.task, request.context, request.budget_usd)
    )

    # Wait for the engine to create the emitter (happens in the first few ms).
    for _ in range(20):
        if stream_registry.get(run_id):
            break
        await asyncio.sleep(0.05)

    return RunResponse(run_id=run_id)


@router.get("/runs/{run_id}/stream")
async def stream_run(run_id: str):
    # connect() checks Redis on a local-cache miss (Redis backend), so a
    # replica that did not create the run can still stream its events.
    emitter = await stream_registry.connect(run_id)
    if emitter is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")
    return EventSourceResponse(emitter)


@router.post("/runs/{run_id}/input")
async def provide_run_input(run_id: str, response: HumanInputResponse):
    """Deliver a human response to a paused run (e.g. approve/reject a budget increase)."""
    # connect() checks Redis on a local-cache miss so that a replica that did
    # not start the run can still deliver the response cross-replica.
    ctx = await context_store.connect(run_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found or not active")
    if not await ctx.provide_human_input(response):
        raise HTTPException(status_code=409, detail="No input is currently pending for this run")
    return {"status": "accepted"}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    """Cancel an active run. Returns 404 if the run is not found or already finished."""
    engine = _get_engine()
    if not engine.cancel_run(run_id):
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found or already finished")
    return {"status": "cancelled"}


@router.post("/runs/{run_id}/followup", response_model=RunResponse)
async def followup_run(run_id: str, request: FollowUpRequest):
    """Start a new run that receives the completed run's report and results as context."""
    run_dir = _run_dir(run_id)
    if not run_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")

    prior_context: dict = dict(request.context)
    prior_context["prior_run_id"] = run_id

    meta = _load_meta(run_dir)
    if meta:
        prior_context["prior_task"] = meta.task

    report_file = run_dir / "report.md"
    if report_file.exists():
        prior_context["prior_report"] = report_file.read_text(encoding="utf-8")

    results_file = run_dir / "results.jsonl"
    if results_file.exists():
        prior_context["prior_subtask_outputs"] = [
            {
                "subtask_id": entry["subtask_id"],
                "agent_id": entry.get("agent_id", "unknown"),
                "output": entry.get("output", {}).get("text", ""),
            }
            for line in results_file.read_text().splitlines()
            if line.strip()
            for entry in (json.loads(line),)
            if "subtask_id" in entry
        ]

    new_run_id = str(uuid.uuid4())
    engine = _get_engine()
    asyncio.create_task(
        engine.run(new_run_id, request.task, prior_context, request.budget_usd)
    )

    for _ in range(20):
        if stream_registry.get(new_run_id):
            break
        await asyncio.sleep(0.05)

    return RunResponse(run_id=new_run_id)


@router.post("/runs/{run_id}/message")
async def send_run_message(run_id: str, message: UserMessage):
    """Inject a user message into the active agent loop of a running run."""
    emitter = await stream_registry.connect(run_id)
    if emitter is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found or not active")
    if emitter.done:
        raise HTTPException(status_code=409, detail="Run has already finished")
    ctx = await context_store.connect(run_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found or not active")
    await ctx.push_user_message(message.content) # type: ignore
    return {"status": "queued"}


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


async def _run_info(d: Path) -> RunInfo:
    meta = _load_meta(d)
    run_id = d.name
    # connect() checks Redis on a local-cache miss so that cross-replica and
    # in-memory paths both return accurate is_streaming / is_awaiting_input.
    emitter = await stream_registry.connect(run_id)
    ctx = await context_store.connect(run_id)
    return RunInfo(
        run_id=run_id,
        has_events=(d / "events.jsonl").exists(),
        has_results=(d / "results.jsonl").exists(),
        has_report=(d / "report.md").exists(),
        has_artifacts=(d / "artifacts.jsonl").exists(),
        is_streaming=emitter is not None and not emitter.done,
        is_awaiting_input=ctx.is_awaiting_input if ctx else False,
        task=meta.task if meta else None,
        name=meta.name if meta else None,
        created_at=meta.created_at if meta else None,
    )


@router.get("/runs", response_model=RunListResponse)
async def list_runs():
    runs_dir = Path(settings.runs_dir)
    if not runs_dir.exists():
        return RunListResponse(runs=[])
    dirs = [d for d in runs_dir.iterdir() if d.is_dir()]

    async def _run_info_guarded(d: Path) -> RunInfo:
        async with _LIST_RUNS_SEM:
            return await _run_info(d)

    runs = list(await asyncio.gather(*[_run_info_guarded(d) for d in dirs]))
    runs.sort(key=lambda r: r.created_at or "", reverse=True)
    return RunListResponse(runs=runs)


@router.get("/runs/{run_id}", response_model=RunInfo)
async def get_run(run_id: str):
    d = _require_run(run_id)
    return await _run_info(d)


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


def _load_artifacts(d: Path) -> list[RunArtifact]:
    artifacts_file = d / "artifacts.jsonl"
    if not artifacts_file.exists():
        return []
    return [
        RunArtifact.model_validate(json.loads(line))
        for line in artifacts_file.read_text().splitlines()
        if line.strip()
    ]


@router.get("/runs/{run_id}/artifacts", response_model=RunArtifactsResponse)
async def get_run_artifacts(run_id: str):
    d = _require_run(run_id)
    return RunArtifactsResponse(run_id=run_id, artifacts=_load_artifacts(d))


@router.get("/runs/{run_id}/artifacts/{artifact_id}", response_model=RunArtifactContentResponse)
async def get_run_artifact_content(run_id: str, artifact_id: str):
    d = _require_run(run_id)
    artifacts = _load_artifacts(d)
    artifact = next((a for a in artifacts if a.id == artifact_id), None)
    if artifact is None:
        raise HTTPException(status_code=404, detail=f"Artifact {artifact_id!r} not found")

    from agentflow.config import settings
    workspace = Path(settings.workspace_dir).resolve()
    target = (workspace / artifact.path).resolve()
    if not str(target).startswith(str(workspace)):
        raise HTTPException(status_code=400, detail="Invalid artifact path")
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"Artifact file not found: {artifact.path}")

    return RunArtifactContentResponse(
        run_id=run_id,
        artifact_id=artifact_id,
        name=artifact.name,
        path=artifact.path,
        content=target.read_text(encoding="utf-8"),
    )
