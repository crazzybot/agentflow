"""Campaigns: long-horizon work as a sequence of fresh-context sessions.

Modelled on the initializer + coding-agent harness pattern, with the ledger
enforced in code rather than by prompt:

1. **Define.** A read-only feature planner turns the task into an ordered
   ledger of features, each with file-checkable ``checks`` and — unless the
   campaign pins one agent — routed to the agent that implements it, as the run
   planner routes subtasks. The ledger lives in
   ``<runs_dir>/campaigns/<id>/features.json`` — outside every agent workspace —
   and its definitions never change afterwards.
2. **Sessions.** Each session is an ordinary run (fresh context, its own session
   log, checkpoints and resume) with a *preset* one-subtask plan: implement the
   next feature, with the feature's checks as acceptance criteria. Only the
   independent verifier's definitive pass marks a feature passing; the agent
   cannot.
3. **Regressions.** Before each session the least recently verified passing
   features are re-checked by the verifier (read-only); a regression is fixed
   next.
4. **Bounds.** Sessions stop at ``max_sessions``, the campaign budget, or when
   only blocked features remain (``campaign_max_feature_attempts`` per feature).

Campaign state is durable: a restart pauses running campaigns, and resuming one
first wakes (or awaits) the interrupted session run, then absorbs its outcome.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Coroutine
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from agentflow.agents.agent import Agent
from agentflow.config import settings
from agentflow.core import lease
from agentflow.core.models import (
    AgentManifest,
    AgentOutput,
    AgentResult,
    AgentStatus,
    Campaign,
    CampaignLimitsUpdate,
    CampaignRequest,
    CampaignStatus,
    CampaignView,
    ExecutionPlan,
    Feature,
    FeatureStatus,
    IterationLimitAction,
    Subtask,
    TaskEnvelope,
)
from agentflow.core.registry import AgentRegistry
from agentflow.core.session import RunSession, _current_session, read_session, replay
from agentflow.core.workspace import (
    _current_workspace,
    run_workspace_rel,
    runs_root,
    validate_workspace_name,
    workspace_path,
)
from agentflow.orchestrator.planner import _parse_criteria
from agentflow.orchestrator.resume import RunNotResumableError
from agentflow.orchestrator.stream import StreamEmitter
from agentflow.orchestrator.verifier import verify_subtask

if TYPE_CHECKING:
    from agentflow.llm import LLMClient
    from agentflow.orchestrator.engine import OrchestratorEngine

logger = logging.getLogger(__name__)

T = TypeVar("T")

SESSION_SUBTASK_ID = "feature"
_TERMINAL = {CampaignStatus.completed, CampaignStatus.stopped, CampaignStatus.failed}


class CampaignNotFoundError(Exception):
    pass


class CampaignActiveError(Exception):
    """Raised when starting or resuming a campaign that is already being driven."""


class CampaignStateError(Exception):
    """Raised when an operation does not fit the campaign's status."""


class UnknownAgentError(Exception):
    """Raised when a request names an agent that is not registered."""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def _campaigns_root() -> Path:
    return runs_root() / "campaigns"


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class CampaignStore:
    """campaign.json + features.json per campaign. Only the runner writes them."""

    def dir(self, campaign_id: str) -> Path:
        return _campaigns_root() / campaign_id

    def exists(self, campaign_id: str) -> bool:
        return (self.dir(campaign_id) / "campaign.json").exists()

    def load(self, campaign_id: str) -> Campaign:
        path = self.dir(campaign_id) / "campaign.json"
        if not path.exists():
            raise CampaignNotFoundError(f"Campaign {campaign_id!r} not found")
        return Campaign.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, campaign: Campaign) -> None:
        d = self.dir(campaign.campaign_id)
        d.mkdir(parents=True, exist_ok=True)
        _write_atomic(d / "campaign.json", campaign.model_dump_json(indent=2))

    def features(self, campaign_id: str) -> list[Feature]:
        path = self.dir(campaign_id) / "features.json"
        if not path.exists():
            return []
        features = [Feature.model_validate(f) for f in json.loads(path.read_text(encoding="utf-8"))]
        for f in features:
            # Ledgers written before per-feature session history only know the last one.
            if not f.sessions and f.last_run_id:
                f.sessions = [f.last_run_id]
        return features

    def save_features(self, campaign_id: str, features: list[Feature]) -> None:
        existing = {f.id: f for f in self.features(campaign_id)}
        for f in features:
            old = existing.get(f.id)
            # Definitions are immutable once written: progress may change, scope may not.
            if old is not None and (old.title, old.description, old.checks) != (f.title, f.description, f.checks):
                raise CampaignStateError(f"Feature {f.id} definition cannot change")
        if existing and set(existing) != {f.id for f in features}:
            raise CampaignStateError("Features cannot be added or removed after definition")
        _write_atomic(
            self.dir(campaign_id) / "features.json",
            json.dumps([f.model_dump(mode="json") for f in features], indent=2),
        )

    def view(self, campaign_id: str) -> CampaignView:
        features = self.features(campaign_id)
        return CampaignView(
            campaign=self.load(campaign_id),
            features=features,
            passing=sum(f.status == FeatureStatus.passing for f in features),
            total=len(features),
        )

    def all_ids(self) -> list[str]:
        root = _campaigns_root()
        if not root.exists():
            return []
        return sorted(p.name for p in root.iterdir() if (p / "campaign.json").exists())


# ---------------------------------------------------------------------------
# Feature definition
# ---------------------------------------------------------------------------

_DEFINER_PROMPT = """\
You turn a project specification into an ordered ledger of features for a team
that will implement them one per working session, each session starting with no
memory of the previous ones.

Explore the workspace first (read-only: file_read, bash_exec_readonly) — it may
already contain part of the project. Then output ONLY a JSON object:
{
  "features": [
    {
      "title": "short name",
      "description": "what to build, precise enough to implement without the spec",
      "checks": ["file-checkable statement", "..."]
    }
  ]
}

Rules:
- Order by dependency: project skeleton and foundations first.
- Each feature must be completable in one focused session (a few files).
- Each feature has 1-5 checks that a reviewer can confirm by READING files, e.g.
  "src/api/routes.py defines POST /items", "README.md documents how to run tests".
  Never require running code, the network, or taste.
- Cover the whole specification; include features already present in the workspace
  only if they still need work.
- At most {max_features} features.
"""

_ROUTING_PROMPT = """
Agent selection — each feature also has "agentId": the agent that implements it,
chosen from the <available_agents> list in your task message:
- Match each feature to the agent whose domain, capabilities and tools best fit the
  work (code to a coding agent, prose documentation to a writing agent, and so on).
- Prefer a specialist agent over a generalist when both could do it.
- A feature is implemented by exactly one agent; if it needs two kinds of work, split
  it into two features in dependency order.
"""


async def _run_feature_planner(
    campaign: Campaign, client: LLMClient, emitter: StreamEmitter, max_features: int,
    roster: str | None,
) -> AgentResult:
    system_prompt = _DEFINER_PROMPT.replace("{max_features}", str(max_features))
    instruction = f"<project_spec>\n{campaign.task}\n</project_spec>"
    if roster is not None:
        system_prompt += _ROUTING_PROMPT
        instruction += f"\n\n<available_agents>\n{roster}\n</available_agents>"
    manifest = AgentManifest(
        agent_id="campaign.feature_planner",
        domain="Planning",
        tools=["file_read", "bash_exec_readonly"],
        system_prompt=system_prompt,
        max_iterations=settings.planner_max_iterations,
        on_iteration_limit=IterationLimitAction.finalize,
        model=settings.planner_model,
        # The whole ledger is one final JSON answer, and thinking shares the same
        # output allowance — the default fallback (8k) truncates a 30-feature ledger.
        max_output_tokens=settings.agent_max_tokens_cap,
    )
    envelope = TaskEnvelope(
        parent_run_id=f"campaign-{campaign.campaign_id}",
        agent_id=manifest.agent_id,
        instruction=instruction,
    )
    return await Agent(manifest, client).run(envelope, emitter)


def _feature_list(result: AgentResult) -> list[Any] | None:
    raw = (result.output.structured or {}).get("features")
    if result.status == AgentStatus.failed or not isinstance(raw, list):
        return None
    return raw


def _route(campaign: Campaign, entry: dict[str, Any], fid: str, registry: AgentRegistry | None) -> str:
    """The agent for a newly defined feature: the campaign's pinned agent, else the
    planner's registered choice, else the default agent."""
    if campaign.agent_id:
        return campaign.agent_id
    chosen = entry.get("agentId")
    if isinstance(chosen, str) and registry is not None and registry.get(chosen) is not None:
        return chosen
    logger.warning("[campaign %s] %s: planner chose no registered agent (%r) — using %s",
                   campaign.campaign_id, fid, chosen, settings.campaign_default_agent)
    return settings.campaign_default_agent


async def define_features(
    campaign: Campaign, client: LLMClient, emitter: StreamEmitter, registry: AgentRegistry | None = None,
) -> list[Feature]:
    """Run the read-only feature planner and return a validated ledger (may be empty).

    Unless the campaign pins one agent, the planner also routes each feature to an
    agent from *registry*, as the run planner routes subtasks.

    If the planner's answer is cut off by max_tokens, it is retried once asking for
    half as many features, since a shorter ledger is the only way to fit the answer.
    """
    roster = registry.summary() if registry is not None and not campaign.agent_id else None
    max_features = settings.campaign_max_features
    result = await _run_feature_planner(campaign, client, emitter, max_features, roster)
    raw = _feature_list(result)
    if raw is None and result.hit_max_tokens:
        max_features = max(1, max_features // 2)
        logger.warning(
            "[campaign %s] Feature planner output was truncated by max_tokens — "
            "retrying with at most %d features", campaign.campaign_id, max_features,
        )
        result = await _run_feature_planner(campaign, client, emitter, max_features, roster)
        raw = _feature_list(result)
    if raw is None:
        logger.warning(
            "[campaign %s] Feature planner produced no feature list (status=%s, truncated=%s)",
            campaign.campaign_id, result.status, result.hit_max_tokens,
        )
        return []
    features: list[Feature] = []
    for entry in raw[:max_features]:
        if not isinstance(entry, dict):
            continue
        fid = f"F{len(features) + 1:02d}"  # ids assigned in code, in the planner's order
        checks = _parse_criteria(entry.get("checks"), fid)
        title, description = str(entry.get("title", "")).strip(), str(entry.get("description", "")).strip()
        if not checks or not title:
            logger.warning("[campaign %s] Dropping feature without title or checks: %r",
                           campaign.campaign_id, entry)
            continue
        features.append(Feature(id=fid, title=title, description=description or title, checks=checks,
                                agent_id=_route(campaign, entry, fid, registry)))
    logger.info("[campaign %s] Feature routing: %s", campaign.campaign_id, [(f.id, f.agent_id) for f in features])
    return features


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

# Directories never worth listing in a session's workspace map.
_MAP_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tool_output", "dist", "build", ".next", "coverage", ".turbo",
})


def _workspace_map(root: Path, max_files: int) -> str:
    """Sorted relative paths of the workspace's files, capped at *max_files*.

    Given to each session so it can open the files its feature needs instead of
    spending its first turns listing directories.
    """
    if not root.is_dir():
        return ""
    paths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _MAP_SKIP_DIRS)
        rel = Path(dirpath).relative_to(root)
        paths.extend(str(rel / f) if rel.parts else f for f in filenames)
    paths.sort()
    listed = "\n".join(paths[:max_files])
    if len(paths) > max_files:
        listed += f"\n… and {len(paths) - max_files} more files"
    return listed


def _session_instruction(
    campaign: Campaign, features: list[Feature], target: Feature, workspace_map: str = "",
) -> str:
    passing = [f for f in features if f.status == FeatureStatus.passing]
    remaining = [f for f in features if f.status != FeatureStatus.passing]
    ledger = "\n".join(
        f"- {f.id} [{f.status.value}] {f.title}" + (f" ({f.agent_id})" if f.agent_id else "") for f in features
    )
    checks = "\n".join(f"- {c}" for c in target.checks)
    previous = next(
        (f.notes for f in sorted(features, key=lambda f: f.last_checked_session, reverse=True) if f.notes),
        "",
    )
    notes_block = f"\n<previous_session_notes>\n{previous}\n</previous_session_notes>\n" if previous else ""
    retry = (
        f"\nThis feature has been attempted {target.attempts} time(s) without passing verification. "
        f"Last notes for it:\n{target.notes}\n" if target.attempts and target.notes else ""
    )
    map_block = f"\n<workspace_files>\n{workspace_map}\n</workspace_files>\n" if workspace_map else ""
    guidance = (
        f"\n<operator_guidance>\nA human reviewing earlier attempts at this feature says:\n"
        f"{target.guidance}\n</operator_guidance>\n" if target.guidance else ""
    )
    return (
        "You are one session of a long-running project. You start with no memory of earlier "
        "sessions; the workspace and the notes below are all that carries over.\n\n"
        f"<project_spec>\n{campaign.task}\n</project_spec>\n\n"
        f"<progress>{len(passing)}/{len(features)} features independently verified passing; "
        f"{len(remaining)} remaining.\n{ledger}\n</progress>\n"
        f"{notes_block}{map_block}\n"
        f'<this_session feature="{target.id}">\n{target.title}\n\n{target.description}\n\n'
        f"An independent reviewer will check, by reading the workspace:\n{checks}\n</this_session>\n"
        f"{retry}{guidance}\n"
        "Rules:\n"
        "- Orient briefly: read the README/notes, then only the files this feature needs "
        "(the workspace files are listed above — no need to explore the tree). Prefer documents "
        "earlier sessions wrote over re-surveying the original sources.\n"
        "- Change existing files with targeted edits (file_write mode='replace_text') rather "
        "than rewriting them.\n"
        "- Implement ONLY this feature, completely. Keep every passing feature working.\n"
        "- Leave the workspace consistent; update the README if behaviour changed.\n"
        "- Finish with a short summary: what you did, anything unfinished, and what the next "
        "session (which starts fresh) needs to know."
    )


class CampaignManager:
    """Starts, drives, pauses and resumes campaigns in this process."""

    def __init__(self, engine: OrchestratorEngine, store: CampaignStore | None = None) -> None:
        self.engine = engine
        self.store = store or CampaignStore()
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def is_active(self, campaign_id: str) -> bool:
        task = self._tasks.get(campaign_id)
        return task is not None and not task.done()

    # -- public operations ---------------------------------------------------

    def create(self, request: CampaignRequest) -> Campaign:
        # A pinned agent runs every feature; otherwise the default agent must exist to
        # take features the planner leaves unrouted.
        agent_id = request.agent_id or settings.campaign_default_agent
        if self.engine.registry.get(agent_id) is None:
            raise UnknownAgentError(f"Unknown agent {agent_id!r}")
        campaign_id = uuid.uuid4().hex[:12]
        name = request.workspace or f"campaign-{campaign_id}"
        validate_workspace_name(name)
        campaign = Campaign(
            campaign_id=campaign_id,
            task=request.task,
            workspace=run_workspace_rel(campaign_id, name),
            agent_id=request.agent_id,
            max_sessions=request.max_sessions,
            budget_usd=request.budget_usd,
            # An explicit null in the request means no per-session cap.
            session_budget_usd=(
                request.session_budget_usd
                if "session_budget_usd" in request.model_fields_set
                else settings.campaign_default_session_budget_usd
            ),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self.store.save(campaign)
        return campaign

    def start(self, campaign_id: str) -> asyncio.Task[None]:
        if self.is_active(campaign_id):
            raise CampaignActiveError(f"Campaign {campaign_id!r} is already running")
        campaign = self.store.load(campaign_id)
        if campaign.status in _TERMINAL and campaign.status != CampaignStatus.stopped:
            raise CampaignStateError(f"Campaign {campaign_id!r} is {campaign.status.value}")
        features = self.store.features(campaign_id)
        if features and not campaign.pending_session:
            # Without this a resume at a limit starts the loop, which stops again at
            # once — the "Resume does nothing" symptom. Say everything to change instead.
            reasons = self._stop_reasons(campaign, features)
            if reasons:
                problems = "; ".join(r for _, r in reasons)
                fixes = " ".join(h for _, r in reasons if (h := self._unblock_hint(r)))
                raise CampaignStateError(f"Cannot resume: {problems}. {fixes}".strip())
        task = asyncio.create_task(self._drive(campaign_id))
        self._tasks[campaign_id] = task
        return task

    def update_limits(self, campaign_id: str, update: CampaignLimitsUpdate) -> Campaign:
        """Change max_sessions / budgets. Only fields present in the request change;
        an explicit null removes a budget. Safe while the campaign runs: the loop
        re-reads the campaign before every decision."""
        campaign = self.store.load(campaign_id)
        if campaign.status in (CampaignStatus.completed, CampaignStatus.failed):
            raise CampaignStateError(f"Campaign {campaign_id!r} is {campaign.status.value}; limits no longer apply")
        for field in update.model_fields_set:
            value = getattr(update, field)
            if field == "max_sessions" and value is None:
                raise CampaignStateError("max_sessions cannot be removed, only changed")
            setattr(campaign, field, value)
        self.store.save(campaign)
        logger.info("[campaign %s] Limits updated: %s", campaign_id,
                    {f: getattr(campaign, f) for f in update.model_fields_set})
        return campaign

    def retry_feature(
        self, campaign_id: str, feature_id: str, guidance: str = "", agent_id: str | None = None,
    ) -> Feature:
        """Give a blocked or failing feature fresh attempts, optionally with operator
        guidance for its next session and a different agent. Its definition and checks
        stay fixed, and only the verifier can still mark it passing (ADR-008)."""
        self.store.load(campaign_id)  # 404 for an unknown campaign
        if agent_id is not None and self.engine.registry.get(agent_id) is None:
            raise UnknownAgentError(f"Unknown agent {agent_id!r}")
        features = self.store.features(campaign_id)
        feature = next((f for f in features if f.id == feature_id), None)
        if feature is None:
            raise CampaignNotFoundError(f"Feature {feature_id!r} not found in campaign {campaign_id!r}")
        if feature.status not in (FeatureStatus.blocked, FeatureStatus.failing):
            raise CampaignStateError(f"Feature {feature_id} is {feature.status.value}; only blocked or failing features can be retried")
        feature.status, feature.attempts = FeatureStatus.failing, 0  # failing features go first
        if guidance.strip():
            feature.guidance = guidance.strip()
        if agent_id is not None:
            feature.agent_id = agent_id
        self.store.save_features(campaign_id, features)
        logger.info("[campaign %s] %s reset for retry on %s%s", campaign_id, feature_id, feature.agent_id,
                    " with operator guidance" if feature.guidance else "")
        return feature

    @staticmethod
    def _unblock_hint(reason: str) -> str:
        if reason.startswith("reached max_sessions"):
            return "Raise max_sessions, then resume."
        if reason.startswith("budget"):
            return "Raise or remove the budget, then resume."
        if reason.startswith("only blocked"):
            return "Retry a blocked feature (optionally with guidance), then resume."
        return ""

    def request_pause(self, campaign_id: str) -> Campaign:
        """Graceful: the campaign pauses after its current session finishes."""
        campaign = self.store.load(campaign_id)
        if not self.is_active(campaign_id):
            raise CampaignStateError(f"Campaign {campaign_id!r} is not running")
        campaign.pause_requested = True
        self.store.save(campaign)
        return campaign

    async def mark_interrupted(self) -> list[str]:
        """Startup: campaigns left running by a dead process become paused.

        Skips campaigns whose lease is live on another replica (still being driven).
        """
        paused = []
        for cid in self.store.all_ids():
            campaign = self.store.load(cid)
            if await lease.run_leases.held_elsewhere(f"campaign-{cid}"):
                continue
            if campaign.status in (CampaignStatus.running, CampaignStatus.defining):
                campaign.status = CampaignStatus.paused
                campaign.stop_reason = "service restarted"
                self.store.save(campaign)
                paused.append(cid)
        return paused

    # -- the loop ------------------------------------------------------------

    async def _drive(self, campaign_id: str) -> None:
        lease_id = f"campaign-{campaign_id}"
        if not await lease.run_leases.acquire(lease_id):
            logger.warning("[campaign %s] Driven by another replica — not starting", campaign_id)
            return
        current = asyncio.current_task()
        keep_alive = asyncio.create_task(lease.run_leases.keep_alive(lease_id, current)) if current else None
        try:
            await self._loop(campaign_id)
        except asyncio.CancelledError:
            self._set_status(campaign_id, CampaignStatus.paused, "campaign task cancelled")
            raise
        except Exception as exc:
            logger.exception("[campaign %s] Campaign loop failed", campaign_id)
            self._set_status(campaign_id, CampaignStatus.paused, f"error: {exc}")
        finally:
            if keep_alive is not None:
                keep_alive.cancel()
            await lease.run_leases.release(lease_id)

    def _set_status(self, campaign_id: str, status: CampaignStatus, reason: str) -> Campaign:
        campaign = self.store.load(campaign_id)
        campaign.status, campaign.stop_reason = status, reason
        campaign.pause_requested = False
        self.store.save(campaign)
        logger.info("[campaign %s] %s: %s", campaign_id, status.value, reason)
        return campaign

    async def _loop(self, campaign_id: str) -> None:
        campaign = self.store.load(campaign_id)
        emitter = StreamEmitter(f"campaign-{campaign_id}")

        if not self.store.features(campaign_id):
            campaign.status, campaign.stop_reason = CampaignStatus.defining, ""
            self.store.save(campaign)
            features = await self._in_campaign_context(
                campaign, define_features(campaign, self.engine.client, emitter, self.engine.registry)
            )
            if not features:
                self._set_status(campaign_id, CampaignStatus.failed, "no usable feature ledger")
                return
            self.store.save_features(campaign_id, features)

        campaign = self.store.load(campaign_id)
        campaign.status, campaign.stop_reason = CampaignStatus.running, ""
        self.store.save(campaign)

        if campaign.pending_session:
            await self._finish_interrupted_session(campaign)

        while True:
            campaign = self.store.load(campaign_id)
            features = self.store.features(campaign_id)
            if campaign.pause_requested:
                self._set_status(campaign_id, CampaignStatus.paused, "paused on request")
                return
            reasons = self._stop_reasons(campaign, features)
            if reasons:
                problems = "; ".join(r for _, r in reasons)
                fixes = " ".join(h for _, r in reasons if (h := self._unblock_hint(r)))
                self._set_status(campaign_id, reasons[0][0], f"{problems}. {fixes}" if fixes else problems)
                return

            await self._check_regressions(campaign, features, emitter)
            features = self.store.features(campaign_id)
            target = self._next_feature(features)
            if target is None:  # a regression check can change what is left
                continue

            run_id = str(uuid.uuid4())
            campaign = self.store.load(campaign_id)
            campaign.sessions.append(run_id)
            campaign.pending_session, campaign.pending_feature = run_id, target.id
            self.store.save(campaign)  # before the run: a restart must know about it
            # Recorded on the feature too, so the ledger shows the session in progress.
            features = self.store.features(campaign_id)
            target = next(f for f in features if f.id == target.id)
            target.sessions.append(run_id)
            self.store.save_features(campaign_id, features)

<<<<<<< HEAD
            agent_id = self._feature_agent(campaign, target)
            plan = ExecutionPlan(run_id=run_id, subtasks=[self._session_subtask(campaign, features, target, agent_id)])
            logger.info("[campaign %s] Session %d: %s on %s (%s)",
                        campaign_id, len(campaign.sessions), target.id, agent_id, run_id)
=======
            plan = ExecutionPlan(run_id=run_id, subtasks=[self._session_subtask(campaign, features, target)])
            logger.info("[campaign %s] Session %d: %s (%s)", campaign_id, len(campaign.sessions), target.id, run_id)
>>>>>>> 9cf66a6d8f2e01379210f7c641a4e445227231f8
            await self.engine.run(
                run_id,
                f"Campaign {campaign_id} — {target.id}: {target.title}",
                {"campaign_id": campaign_id, "feature_id": target.id},
                budget_usd=self._session_budget(campaign),
                workspace=campaign.workspace,
                plan=plan,
            )
            if not self._absorb(campaign_id, run_id, target.id):
                return  # session was cancelled: campaign paused

    async def _finish_interrupted_session(self, campaign: Campaign) -> None:
        run_id, feature_id = campaign.pending_session, campaign.pending_feature
        assert run_id is not None and feature_id is not None
        if self.engine.is_active(run_id):  # e.g. already woken by startup auto-resume
            await self.engine.wait_for(run_id)
        else:
            try:
                await self.engine.wake(run_id)
            except RunNotResumableError as exc:
                logger.info("[campaign %s] Not waking %s: %s", campaign.campaign_id, run_id, exc)
        self._absorb(campaign.campaign_id, run_id, feature_id)

    def _absorb(self, campaign_id: str, run_id: str, feature_id: str) -> bool:
        """Fold a finished session run into the ledger. False if the campaign paused."""
        snap = replay(run_id, read_session(run_id))
        campaign = self.store.load(campaign_id)
        features = self.store.features(campaign_id)
        campaign.spent_usd += snap.total_cost_usd
        campaign.pending_session = campaign.pending_feature = None
        session_no = len(campaign.sessions)

        if snap.status == "cancelled":
            self.store.save(campaign)
            self._set_status(campaign_id, CampaignStatus.paused, f"session {run_id} was cancelled")
            return False

        result = snap.results.get(SESSION_SUBTASK_ID)
        feature = next(f for f in features if f.id == feature_id)
        feature.last_run_id = run_id
        if run_id not in feature.sessions:
            feature.sessions.append(run_id)
        feature.last_checked_session = session_no
        if result is not None and result.output.text:
            feature.notes = result.output.text[:2_000]
        verification = result.verification if result is not None else None
        if result is not None and result.status == AgentStatus.success and verification and verification.passed is True:
            feature.status, feature.evidence = FeatureStatus.passing, verification.checks
            feature.guidance = ""  # it served its purpose
        else:
            # Unlike a single run, an inconclusive verdict does not count as a pass here:
            # the ledger only records what was independently confirmed.
            feature.attempts += 1
            feature.evidence = verification.checks if verification else []
            feature.status = (
                FeatureStatus.blocked
                if feature.attempts >= settings.campaign_max_feature_attempts
                else FeatureStatus.failing
            )
        self.store.save_features(campaign_id, features)
        self.store.save(campaign)
        logger.info("[campaign %s] %s → %s after session %d", campaign_id, feature_id, feature.status.value, session_no)
        return True

    async def _check_regressions(self, campaign: Campaign, features: list[Feature], emitter: StreamEmitter) -> None:
        passing = sorted(
            (f for f in features if f.status == FeatureStatus.passing),
            key=lambda f: f.last_checked_session,
        )[: settings.campaign_regression_checks]
        for feature in passing:
            agent_id = self._feature_agent(campaign, feature)
            subtask = Subtask(id=feature.id, agent_id=agent_id,
                              instruction=feature.description, acceptance_criteria=feature.checks)
            claim = AgentResult(task_id=feature.id, agent_id=agent_id, status=AgentStatus.success,
                                output=AgentOutput(text="This feature passed verification in an earlier session; "
                                                        "confirm it still holds."))
            verdict, verifier_result = await self._in_campaign_context(
                campaign, verify_subtask(f"campaign-{campaign.campaign_id}", subtask, claim, self.engine.client, emitter)
            )
            fresh = self.store.load(campaign.campaign_id)
            fresh.spent_usd += verifier_result.cost_usd
            self.store.save(fresh)
            features = self.store.features(campaign.campaign_id)
            current = next(f for f in features if f.id == feature.id)
            current.last_checked_session = len(fresh.sessions)
            if verdict.passed is False:
                logger.warning("[campaign %s] Regression in %s", campaign.campaign_id, feature.id)
                current.status, current.attempts, current.evidence = FeatureStatus.failing, 0, verdict.checks
                unmet = "; ".join(f"{c.criterion} ({c.evidence})" for c in verdict.unmet)
                current.notes = f"REGRESSION found before session {len(fresh.sessions) + 1}: {unmet}"
            self.store.save_features(campaign.campaign_id, features)

    async def _in_campaign_context(self, campaign: Campaign, coro: Coroutine[Any, Any, T]) -> T:
        """Run definition/regression agents in the campaign workspace, logging their
        transcripts to the campaign's own session log."""
        ws_token = _current_workspace.set(workspace_path(campaign.workspace))
        session = RunSession(f"campaign-{campaign.campaign_id}",
                             path=self.store.dir(campaign.campaign_id) / "session.jsonl")
        session_token = _current_session.set(session)
        try:
            return await coro
        finally:
            _current_session.reset(session_token)
            _current_workspace.reset(ws_token)

    # -- policy --------------------------------------------------------------

    def _feature_agent(self, campaign: Campaign, feature: Feature) -> str:
        """The agent a feature's sessions run on: its routed agent while that is still
        registered (a manifest may have been removed since definition), else the
        campaign's pinned agent, else the default agent."""
        if feature.agent_id and self.engine.registry.get(feature.agent_id) is not None:
            return feature.agent_id
        fallback = campaign.agent_id or settings.campaign_default_agent
        if feature.agent_id:
            logger.warning("[campaign %s] %s: agent %r is no longer registered — using %s",
                           campaign.campaign_id, feature.id, feature.agent_id, fallback)
        return fallback

    @staticmethod
    def _session_subtask(campaign: Campaign, features: list[Feature], target: Feature, agent_id: str) -> Subtask:
        """The one-subtask plan of a session. A feature's first attempt runs on the
        cheaper session model and effort; a failing one (failed a session or regressed)
        escalates to the escalation model and effort."""
        escalate = target.status == FeatureStatus.failing
        model = settings.campaign_escalation_model if escalate else settings.campaign_session_model
        effort = (
            settings.campaign_escalation_thinking_effort if escalate
            else settings.campaign_session_thinking_effort
        )
        workspace_map = _workspace_map(workspace_path(campaign.workspace), settings.campaign_workspace_map_max_files)
        return Subtask(
            id=SESSION_SUBTASK_ID,
            agent_id=agent_id,
            instruction=_session_instruction(campaign, features, target, workspace_map),
            acceptance_criteria=target.checks,
            model=model or None,
            thinking_effort=effort or None,
            max_iterations=settings.campaign_session_max_iterations,
        )

    @staticmethod
    def _session_subtask(campaign: Campaign, features: list[Feature], target: Feature) -> Subtask:
        """The one-subtask plan of a session. A feature's first attempt runs on the
        cheaper session model and effort; a failing one (failed a session or regressed)
        escalates to the escalation model and effort."""
        escalate = target.status == FeatureStatus.failing
        model = settings.campaign_escalation_model if escalate else settings.campaign_session_model
        effort = (
            settings.campaign_escalation_thinking_effort if escalate
            else settings.campaign_session_thinking_effort
        )
        workspace_map = _workspace_map(workspace_path(campaign.workspace), settings.campaign_workspace_map_max_files)
        return Subtask(
            id=SESSION_SUBTASK_ID,
            agent_id=campaign.agent_id,
            instruction=_session_instruction(campaign, features, target, workspace_map),
            acceptance_criteria=target.checks,
            model=model or None,
            thinking_effort=effort or None,
            max_iterations=settings.campaign_session_max_iterations,
        )

    @staticmethod
    def _next_feature(features: list[Feature]) -> Feature | None:
        for wanted in (FeatureStatus.failing, FeatureStatus.pending):
            for f in features:
                if f.status == wanted:
                    return f
        return None

    @staticmethod
    def _stop(campaign: Campaign, features: list[Feature]) -> tuple[CampaignStatus, str] | None:
        reasons = CampaignManager._stop_reasons(campaign, features)
        return reasons[0] if reasons else None

    @staticmethod
    def _stop_reasons(campaign: Campaign, features: list[Feature]) -> list[tuple[CampaignStatus, str]]:
        """Every condition that keeps the campaign from running another session.

        Several can hold at once (a blocked feature *and* the session limit *and* the
        budget); resume reports all of them so the operator fixes them in one go.
        """
        if all(f.status == FeatureStatus.passing for f in features):
            return [(CampaignStatus.completed, f"all {len(features)} features passing")]
        reasons: list[tuple[CampaignStatus, str]] = []
        if CampaignManager._next_feature(features) is None:
            blocked = [f.id for f in features if f.status == FeatureStatus.blocked]
            reasons.append((CampaignStatus.stopped, f"only blocked features remain: {blocked}"))
        if len(campaign.sessions) >= campaign.max_sessions:
            reasons.append((CampaignStatus.stopped, f"reached max_sessions ({campaign.max_sessions})"))
        if campaign.budget_usd is not None and campaign.spent_usd >= campaign.budget_usd:
            reasons.append((CampaignStatus.stopped,
                            f"budget ${campaign.budget_usd:.2f} spent (${campaign.spent_usd:.4f})"))
        return reasons

    @staticmethod
    def _session_budget(campaign: Campaign) -> float | None:
        if campaign.budget_usd is None:
            return campaign.session_budget_usd
        remaining = max(campaign.budget_usd - campaign.spent_usd, 0.0)
        return min(campaign.session_budget_usd or remaining, remaining)
