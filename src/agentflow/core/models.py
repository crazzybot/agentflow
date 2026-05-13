"""All Pydantic models for message protocols, manifests, and SSE events."""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Agent manifest
# ---------------------------------------------------------------------------


class MCPServerConfig(BaseModel):
    name: str
    url: str  # SSE endpoint, e.g. "http://localhost:3001/sse"
    transport: str = "sse"


class AgentManifest(BaseModel):
    agent_id: str
    version: str = "1.0.0"
    domain: str
    capabilities: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    system_prompt: str
    fallback_for: list[str] = Field(default_factory=list)
    max_concurrency: int = 3


# ---------------------------------------------------------------------------
# Task Envelope  (Orchestrator → Agent)
# ---------------------------------------------------------------------------


class TaskConstraints(BaseModel):
    max_tokens: int = 4096
    timeout_ms: int = 30_000


class TaskContext(BaseModel):
    prior_results: dict[str, Any] = Field(default_factory=dict)
    shared_memory: dict[str, Any] = Field(default_factory=dict)


class TaskEnvelope(BaseModel):
    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    parent_run_id: str
    agent_id: str
    instruction: str
    context: TaskContext = Field(default_factory=TaskContext)
    constraints: TaskConstraints = Field(default_factory=TaskConstraints)


# ---------------------------------------------------------------------------
# Agent Result  (Agent → Orchestrator)
# ---------------------------------------------------------------------------


class AgentStatus(str, Enum):
    success = "success"
    partial = "partial"
    failed = "failed"


class AgentOutput(BaseModel):
    structured: dict[str, Any] = Field(default_factory=dict)
    text: str = ""


class AgentResult(BaseModel):
    task_id: str
    agent_id: str
    status: AgentStatus
    output: AgentOutput = Field(default_factory=AgentOutput)
    error: str | None = None
    tokens_used: int = 0
    duration_ms: int = 0


# ---------------------------------------------------------------------------
# Back-channel request  (Agent → Orchestrator)
# ---------------------------------------------------------------------------


class InfoRequest(BaseModel):
    type: str = "info_request"
    from_agent: str
    task_id: str
    query: str
    required_fields: list[str] = Field(default_factory=list)
    blocking: bool = True


# ---------------------------------------------------------------------------
# Subtask plan (produced by LLM planner)
# ---------------------------------------------------------------------------


class Subtask(BaseModel):
    id: str
    agent_id: str
    instruction: str
    depends_on: list[str] = Field(default_factory=list)
    expected_output: str = ""


class ExecutionPlan(BaseModel):
    run_id: str
    subtasks: list[Subtask]


# ---------------------------------------------------------------------------
# SSE Stream events  (Server → Client)
# ---------------------------------------------------------------------------


class SSEEventType(str, Enum):
    run_started = "run:started"
    plan_created = "plan:created"
    task_dispatched = "task:dispatched"
    agent_progress = "agent:progress"
    agent_query = "agent:query"
    task_complete = "task:complete"
    task_failed = "task:failed"
    run_complete = "run:complete"
    run_error = "run:error"


class SSEPayload(BaseModel):
    message: str = ""
    partial: Any = None
    data: Any = None


class SSEEvent(BaseModel):
    run_id: str
    seq: int
    ts: int = Field(default_factory=lambda: int(time.time() * 1000))
    type: SSEEventType
    agent_id: str | None = None
    payload: SSEPayload = Field(default_factory=SSEPayload)


# ---------------------------------------------------------------------------
# HTTP request/response shapes
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    task: str
    context: dict[str, Any] = Field(default_factory=dict)


class RunResponse(BaseModel):
    run_id: str
    status: str = "started"
