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
    transport: str = "sse"
    # SSE transport
    url: str | None = None  # e.g. "http://localhost:3001/sse"
    # stdio transport
    command: str | None = None  # executable to launch, e.g. "uv" or "skb-mcp"
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)  # merged on top of current env


class AgentManifest(BaseModel):
    agent_id: str
    version: str = "1.0.0"
    domain: str
    capabilities: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    system_prompt: str
    decomposition_prompt: str | None = None
    fallback_for: list[str] = Field(default_factory=list)
    max_concurrency: int = 3
    max_iterations: int | None = None  # None → fall back to settings.agent_max_iterations
    tool_limits: dict[str, int] | None = None  # per-task call budgets e.g. {"fetch_url": 5}
    thinking_budget_tokens: int | None = None  # enables extended thinking; min 1024, must be < max_tokens


# ---------------------------------------------------------------------------
# Task Envelope  (Orchestrator → Agent)
# ---------------------------------------------------------------------------


class TaskConstraints(BaseModel):
    budget_usd: float | None = None  # per-task budget; None → use token/iteration fallbacks
    timeout_ms: int = 300_000


class TaskContext(BaseModel):
    prior_results: dict[str, Any] = Field(default_factory=dict)
    shared_memory: dict[str, Any] = Field(default_factory=dict)
    user_context: dict[str, Any] = Field(default_factory=dict)
    # Full message history from a single dependency — injected when there is exactly one
    # upstream subtask so the agent inherits prior tool results without re-reading files.
    prior_messages: list[Any] = Field(default_factory=list, exclude=True)


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
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0        # extended thinking tokens (subset of output_tokens)
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    tokens_used: int = 0  # kept for backward compat; equals sum of all token types
    cost_usd: float = 0.0
    duration_ms: int = 0
    # Full conversation messages — not serialised, used in-memory for continuation
    # (Fix 3) and downstream context injection (Fix 2).
    messages: list[Any] = Field(default_factory=list, exclude=True)


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
    budget_fraction: float | None = None  # share of the total run budget for this subtask


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
    agent_thought = "agent:thought"
    agent_query = "agent:query"
    task_complete = "task:complete"
    task_partial = "task:partial"
    task_failed = "task:failed"
    task_continuing = "task:continuing"
    run_complete = "run:complete"
    run_error = "run:error"
    run_budget_exceeded = "run:budget_exceeded"
    run_awaiting_input = "run:awaiting_input"
    run_cancelled = "run:cancelled"
    run_message_received = "run:message_received"


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
# Human-in-the-loop input request/response
# ---------------------------------------------------------------------------


class HumanInputRequest(BaseModel):
    request_type: str  # e.g. "task_budget_exhausted", "run_budget_exhausted"
    message: str
    context: dict[str, Any] = Field(default_factory=dict)


class HumanInputResponse(BaseModel):
    action: str  # "continue" or "cancel"
    budget_increase_usd: float | None = None


# ---------------------------------------------------------------------------
# HTTP request/response shapes
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    task: str
    context: dict[str, Any] = Field(default_factory=dict)
    budget_usd: float | None = None


class FollowUpRequest(BaseModel):
    task: str
    context: dict[str, Any] = Field(default_factory=dict)
    budget_usd: float | None = None


class UserMessage(BaseModel):
    content: str


class RunResponse(BaseModel):
    run_id: str
    status: str = "started"


# ---------------------------------------------------------------------------
# Past-run query response shapes
# ---------------------------------------------------------------------------


class RunMeta(BaseModel):
    run_id: str
    task: str
    name: str
    created_at: str


class RunInfo(BaseModel):
    run_id: str
    has_events: bool
    has_results: bool
    has_report: bool
    has_artifacts: bool = False
    is_streaming: bool = False
    is_awaiting_input: bool = False
    task: str | None = None
    name: str | None = None
    created_at: str | None = None


class RunListResponse(BaseModel):
    runs: list[RunInfo]


class SubtaskResult(AgentResult):
    subtask_id: str


class RunResultsResponse(BaseModel):
    run_id: str
    results: list[SubtaskResult]


class RunEventsResponse(BaseModel):
    run_id: str
    events: list[SSEEvent]


class RunReportResponse(BaseModel):
    run_id: str
    report: str


class RunArtifact(BaseModel):
    id: str
    name: str
    path: str  # relative to workspace root


class RunArtifactsResponse(BaseModel):
    run_id: str
    artifacts: list[RunArtifact]


class RunArtifactContentResponse(BaseModel):
    run_id: str
    artifact_id: str
    name: str
    path: str
    content: str
