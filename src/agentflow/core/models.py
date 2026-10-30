"""All Pydantic models for message protocols, manifests, and SSE events."""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from agentflow.core.workspace import validate_workspace_name


# ---------------------------------------------------------------------------
# Agent manifest
# ---------------------------------------------------------------------------


class IterationLimitAction(str, Enum):
    """What the agent should do when it hits its max_iterations cap."""
    stop = "stop"        # return partial result immediately (default)
    finalize = "finalize"  # inject a prompt, make one final LLM call (tools disabled) to produce output
    ask_user = "ask_user"  # pause and request user input via HITL before deciding



class MCPServerConfig(BaseModel):
    name: str
    transport: str = "sse"
    # SSE transport
    url: str | None = None  # e.g. "http://localhost:3001/sse"
    # stdio transport
    command: str | None = None  # executable to launch, e.g. "uv" or "skb-mcp"
    args: list[str] = Field(default_factory=list)
    # Literal env values for the stdio process, merged onto the mcp SDK's safe default
    # env (PATH, HOME, USER, ...) — the API process's own env is NOT inherited.
    env: dict[str, str] = Field(default_factory=dict)
    # Names of API-process env vars to forward (e.g. ["GITHUB_TOKEN"]); keeps secrets
    # out of manifest YAML while making each credential exposure an explicit opt-in.
    env_passthrough: list[str] = Field(default_factory=list)


_SHELL_TOOLS = frozenset({"bash_exec", "bash_exec_readonly"})


class ToolPolicyConfig(BaseModel):
    """A pre-tool-use policy declared in a manifest (see tools/policy.py)."""
    tool: str
    # Every command in a shell pipeline/list must be one of these.
    allow_commands: list[str] | None = None

    @field_validator("allow_commands")
    @classmethod
    def _shell_only(cls, v: list[str] | None, info: Any) -> list[str] | None:
        if v is not None and info.data.get("tool") not in _SHELL_TOOLS:
            raise ValueError(f"allow_commands applies only to {sorted(_SHELL_TOOLS)}")
        return v


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
    model: str | None = None  # explicit model-id override; wins over model_tier and settings.agent_model
    # Named cost tier ("economy" | "standard" | "premium"), resolved to a model id via
    # settings.resolve_model_tier(). Lets a whole fleet of manifests be re-tiered by
    # changing three settings instead of editing every manifest's raw model id. Ignored
    # when `model` is set.
    model_tier: str | None = None
    max_concurrency: int = 3
    max_iterations: int | None = None  # None → fall back to settings.agent_max_iterations
    tool_limits: dict[str, int] | None = None  # per-task call budgets e.g. {"fetch_url": 5}
    # Pre-tool-use policies checked before every call, e.g. a command allowlist for
    # bash_exec. See tools/policy.py.
    tool_policies: list[ToolPolicyConfig] = Field(default_factory=list)
    # Enables adaptive extended thinking at this effort level: "low" | "medium" | "high" |
    # "xhigh" | "max". None → fall back to settings.agent_thinking_effort.
    thinking_effort: str | None = None
    on_iteration_limit: IterationLimitAction = IterationLimitAction.stop
    iteration_limit_message: str | None = None  # custom prompt injected for the 'finalize' action


# ---------------------------------------------------------------------------
# Task Envelope  (Orchestrator → Agent)
# ---------------------------------------------------------------------------


class TaskConstraints(BaseModel):
    budget_usd: float | None = None  # per-task budget; None → use token/iteration fallbacks
    timeout_ms: int = 300_000
    # Planner-assigned overrides for this specific subtask instance (from Subtask.model_tier /
    # Subtask.thinking_effort). Take precedence over the target agent's manifest defaults —
    # see Agent._agentic_loop's resolution order — but not over a manifest's explicit `model`.
    model_tier: str | None = None
    thinking_effort: str | None = None


class TaskContext(BaseModel):
    prior_results: dict[str, Any] = Field(default_factory=dict)
    shared_memory: dict[str, Any] = Field(default_factory=dict)
    user_context: dict[str, Any] = Field(default_factory=dict)
    # File paths written by each upstream dependency — agents use these to read
    # upstream outputs rather than receiving the full conversation history.
    upstream_artifacts: dict[str, list[str]] = Field(default_factory=dict)


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


class CriterionCheck(BaseModel):
    criterion: str
    met: bool
    evidence: str = ""


class Verification(BaseModel):
    """An independent verifier's verdict on a subtask's acceptance criteria."""
    # True = all met, False = at least one unmet, None = inconclusive (the verifier
    # failed or produced no verdict; treated as a pass so a broken check never
    # blocks good work).
    passed: bool | None
    checks: list[CriterionCheck] = Field(default_factory=list)
    summary: str = ""
    repairs: int = 0  # repair rounds the agent was given before this verdict

    @property
    def unmet(self) -> list[CriterionCheck]:
        return [c for c in self.checks if not c.met]


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
    # True when the agent stopped because the LLM hit its max_tokens limit.
    # Signals the engine that increasing the budget (→ larger max_tokens) is
    # required before continuation can make progress.
    hit_max_tokens: bool = False
    # Largest full prompt (input + cache read + cache write tokens) of any call in
    # this invocation — the measurement for judging whether context snipping keeps
    # agents bounded (docs/context-optimization-plan.md, Fix 5).
    peak_prompt_tokens: int = 0
    # Set when the subtask had acceptance criteria and was checked by the verifier.
    verification: Verification | None = None
    # Workspace checkpoint commit taken when this subtask finished (None: no
    # changes, or checkpoints disabled). See orchestrator/checkpoints.py.
    checkpoint: str | None = None
    # Relative paths of every file successfully written during this agent's run.
    files_written: list[str] = Field(default_factory=list)
    # Full conversation messages — not serialised, used in-memory for same-agent
    # continuation when the iteration limit is hit.
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
    # Optional per-subtask overrides the planner may set when it judges this specific
    # instance warrants a different cost/quality trade-off than the target agent's
    # manifest default (e.g. downgrading a mechanical formatting pass, or raising effort
    # for an unusually hard instance of a normally-simple agent). None → agent manifest
    # default applies unchanged. See Agent._agentic_loop for resolution order.
    model_tier: str | None = None  # "economy" | "standard" | "premium"
    thinking_effort: str | None = None  # "low" | "medium" | "high" | "xhigh" | "max"
    # Objectively checkable statements an independent verifier confirms against the
    # workspace before the subtask counts as done (orchestrator/verifier.py).
    acceptance_criteria: list[str] = Field(default_factory=list)


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
    agent_tool_result = "agent:tool_result"
    agent_query = "agent:query"
    task_complete = "task:complete"
    task_partial = "task:partial"
    task_failed = "task:failed"
    task_continuing = "task:continuing"
    task_verified = "task:verified"
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
    turn_index: int | None = None
    tool_call_id: str | None = None
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
    iteration_increase: int | None = None  # extra iterations granted for ask_user iteration-limit HITL


# ---------------------------------------------------------------------------
# HTTP request/response shapes
# ---------------------------------------------------------------------------


class _WorkspaceRequest(BaseModel):
    # Named workspace (workspace/<name>/) shared with other runs of the same
    # project; omitted → a fresh per-run workspace (or, for a followup, the
    # prior run's workspace).
    workspace: str | None = None

    @field_validator("workspace")
    @classmethod
    def _check_workspace(cls, v: str | None) -> str | None:
        return None if v is None else validate_workspace_name(v)


class RunRequest(_WorkspaceRequest):
    task: str
    context: dict[str, Any] = Field(default_factory=dict)
    budget_usd: float | None = None


class FollowUpRequest(_WorkspaceRequest):
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
    # Workspace path relative to settings.workspace_dir ("runs/<run_id>" or a
    # named workspace). None for runs recorded before per-run workspaces, which
    # all used the workspace root.
    workspace: str | None = None


class RunInfo(BaseModel):
    run_id: str
    has_events: bool
    has_results: bool
    has_report: bool
    has_artifacts: bool = False
    has_session: bool = False
    is_streaming: bool = False
    is_awaiting_input: bool = False
    task: str | None = None
    name: str | None = None
    created_at: str | None = None
    workspace: str | None = None


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


# ---------------------------------------------------------------------------
# Campaigns: long-horizon work as a sequence of fresh-context sessions
# ---------------------------------------------------------------------------


class FeatureStatus(str, Enum):
    pending = "pending"
    passing = "passing"    # independently verified in the latest check
    failing = "failing"    # attempted (or regressed) and not verified
    blocked = "blocked"    # exhausted its attempts; skipped by later sessions


class Feature(BaseModel):
    """One ledger entry. id/title/description/checks are fixed once defined; only
    the campaign runner changes the rest — agents never write the ledger."""
    id: str
    title: str
    description: str
    checks: list[str]
    status: FeatureStatus = FeatureStatus.pending
    attempts: int = 0
    last_run_id: str | None = None
    last_checked_session: int = 0  # session count when last verified (regression order)
    notes: str = ""                # the last session's own summary for this feature
    evidence: list[CriterionCheck] = Field(default_factory=list)


class CampaignStatus(str, Enum):
    defining = "defining"    # writing the feature ledger
    running = "running"
    paused = "paused"        # by request, a cancelled session, or a restart
    completed = "completed"  # every feature passing
    stopped = "stopped"      # session/budget limit, or only blocked features left
    failed = "failed"        # no usable feature ledger


class Campaign(BaseModel):
    campaign_id: str
    task: str
    workspace: str
    agent_id: str
    max_sessions: int
    budget_usd: float | None = None
    session_budget_usd: float | None = None
    status: CampaignStatus = CampaignStatus.defining
    stop_reason: str = ""
    pause_requested: bool = False
    spent_usd: float = 0.0
    sessions: list[str] = Field(default_factory=list)  # run ids, in order
    # Run started but not yet absorbed into the ledger; set before the run begins
    # so a restart knows which session to wake and absorb.
    pending_session: str | None = None
    pending_feature: str | None = None
    created_at: str


class CampaignRequest(_WorkspaceRequest):
    task: str
    agent_id: str = "CodeAgent"
    max_sessions: int = Field(default=10, ge=1, le=500)
    budget_usd: float | None = Field(default=None, gt=0)
    session_budget_usd: float | None = Field(default=None, gt=0)


class CampaignView(BaseModel):
    campaign: Campaign
    features: list[Feature]
    passing: int
    total: int


class CampaignListResponse(BaseModel):
    campaigns: list[CampaignView]


class RunCheckpoint(BaseModel):
    sha: str
    label: str
    subtask_id: str | None = None
    agent_id: str | None = None
    changed: list[str] = Field(default_factory=list)
    ts: int


class RunCheckpointsResponse(BaseModel):
    run_id: str
    checkpoints: list[RunCheckpoint]


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
