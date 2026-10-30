"""Tests for campaigns: ledger store, feature definition, and the session loop."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from agentflow.config import settings
from agentflow.core.bus import TaskBus
from agentflow.core.context import ContextStore
from agentflow.core.models import (
    AgentManifest,
    AgentOutput,
    AgentResult,
    AgentStatus,
    Campaign,
    CampaignLimitsUpdate,
    CampaignRequest,
    CampaignStatus,
    CriterionCheck,
    Feature,
    FeatureStatus,
    Verification,
)
from agentflow.core.registry import AgentRegistry
from agentflow.core.session import RunSession, SessionEventType
from agentflow.orchestrator.campaign import (
    CampaignManager,
    CampaignStateError,
    CampaignStore,
    _session_instruction,
    define_features,
)
from agentflow.orchestrator.engine import OrchestratorEngine
from agentflow.orchestrator.stream import StreamRegistry


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.runs_dir", str(tmp_path / "runs"))
    monkeypatch.setattr("agentflow.config.settings.workspace_dir", str(tmp_path / "workspace"))
    monkeypatch.setattr("agentflow.config.settings.enable_checkpoints", False)
    return tmp_path


def _features(n: int = 2) -> list[Feature]:
    return [Feature(id=f"F{i:02d}", title=f"feature {i}", description=f"build {i}", checks=[f"check {i}"])
            for i in range(1, n + 1)]


def _campaign(**kw) -> Campaign:
    base = dict(campaign_id="c1", task="build an app", workspace="proj", agent_id="A",
                max_sessions=10, created_at="now")
    return Campaign(**{**base, **kw})


# ---------------------------------------------------------------------------
# Store: the ledger's definitions are immutable
# ---------------------------------------------------------------------------

def test_store_round_trip_and_immutable_definitions(dirs):
    store = CampaignStore()
    store.save(_campaign())
    store.save_features("c1", _features())
    assert store.load("c1").task == "build an app"

    progressed = store.features("c1")
    progressed[0].status, progressed[0].attempts = FeatureStatus.passing, 1
    store.save_features("c1", progressed)  # progress may change
    assert store.features("c1")[0].status == FeatureStatus.passing

    edited = store.features("c1")
    edited[1].checks = ["something easier"]
    with pytest.raises(CampaignStateError, match="cannot change"):
        store.save_features("c1", edited)
    with pytest.raises(CampaignStateError, match="added or removed"):
        store.save_features("c1", store.features("c1")[:1])

    view = store.view("c1")
    assert (view.passing, view.total) == (1, 2)


# ---------------------------------------------------------------------------
# Feature definition
# ---------------------------------------------------------------------------

async def test_define_features_assigns_ids_and_drops_uncheckable(dirs, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.campaign_max_features", 3)
    captured = {}

    class FakePlanner:
        def __init__(self, manifest, client):
            captured["manifest"] = manifest

        async def run(self, envelope, emitter):
            return AgentResult(task_id="x", agent_id="p", status=AgentStatus.success, output=AgentOutput(
                structured={"features": [
                    {"id": "whatever", "title": "skeleton", "description": "d", "checks": ["README.md exists"]},
                    {"title": "no checks", "description": "d", "checks": []},
                    {"title": "api", "checks": ["api.py defines GET /x"]},
                    {"title": "a", "checks": ["c"]},
                    {"title": "over the cap", "checks": ["c"]},
                ]}))

    with patch("agentflow.orchestrator.campaign.Agent", FakePlanner):
        features = await define_features(_campaign(), MagicMock(), MagicMock())

    assert [(f.id, f.title) for f in features] == [("F01", "skeleton"), ("F02", "api")]
    assert features[1].description == "api"  # falls back to the title
    assert set(captured["manifest"].tools) == {"file_read", "bash_exec_readonly"}
    assert captured["manifest"].max_output_tokens == settings.agent_max_tokens_cap


async def test_define_features_retries_smaller_ledger_after_truncation(dirs, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.campaign_max_features", 30)
    prompts: list[str] = []

    class TruncatingPlanner:
        def __init__(self, manifest, client):
            prompts.append(manifest.system_prompt)

        async def run(self, envelope, emitter):
            if len(prompts) == 1:  # first attempt: cut off mid-JSON, nothing parsed
                return AgentResult(task_id="x", agent_id="p", status=AgentStatus.partial,
                                   hit_max_tokens=True, output=AgentOutput(text='{"features": [{"ti'))
            return AgentResult(task_id="x", agent_id="p", status=AgentStatus.success, output=AgentOutput(
                structured={"features": [{"title": "skeleton", "checks": ["README.md exists"]}]}))

    with patch("agentflow.orchestrator.campaign.Agent", TruncatingPlanner):
        features = await define_features(_campaign(), MagicMock(), MagicMock())

    assert [f.title for f in features] == ["skeleton"]
    assert "At most 30 features" in prompts[0] and "At most 15 features" in prompts[1]


async def test_define_features_failure_yields_empty_ledger(dirs):
    class Broken:
        calls = 0

        def __init__(self, *_a):
            Broken.calls += 1

        async def run(self, *_a):
            return AgentResult(task_id="x", agent_id="p", status=AgentStatus.failed, error="nope")

    with patch("agentflow.orchestrator.campaign.Agent", Broken):
        assert await define_features(_campaign(), MagicMock(), MagicMock()) == []
    assert Broken.calls == 1  # a plain failure is not retried


def test_session_instruction_carries_spec_progress_target_and_notes():
    features = _features(3)
    features[0].status, features[0].notes, features[0].last_checked_session = FeatureStatus.passing, "used FastAPI", 1
    features[1].attempts, features[1].notes = 1, "ran out of time on validation"
    text = _session_instruction(_campaign(), features, features[1])
    assert "build an app" in text and "1/3 features" in text
    assert 'feature="F02"' in text and "check 2" in text
    assert "used FastAPI" in text and "attempted 1 time" in text and "ran out of time" in text


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

def test_next_feature_prefers_failing_and_skips_blocked():
    fs = _features(4)
    fs[0].status, fs[1].status, fs[2].status = FeatureStatus.passing, FeatureStatus.blocked, FeatureStatus.failing
    assert CampaignManager._next_feature(fs).id == "F03"
    fs[2].status = FeatureStatus.passing
    assert CampaignManager._next_feature(fs).id == "F04"


def test_stop_conditions():
    fs = _features(2)
    assert CampaignManager._stop(_campaign(), fs) is None
    assert CampaignManager._stop(_campaign(max_sessions=1, sessions=["r"]), fs)[1].startswith("reached max_sessions")
    assert "budget" in CampaignManager._stop(_campaign(budget_usd=1.0, spent_usd=1.0), fs)[1]
    fs[0].status, fs[1].status = FeatureStatus.passing, FeatureStatus.blocked
    assert CampaignManager._stop(_campaign(), fs)[0] == CampaignStatus.stopped
    fs[1].status = FeatureStatus.passing
    assert CampaignManager._stop(_campaign(), fs)[0] == CampaignStatus.completed


def test_session_budget():
    assert CampaignManager._session_budget(_campaign()) is None
    assert CampaignManager._session_budget(_campaign(session_budget_usd=0.5)) == 0.5
    assert CampaignManager._session_budget(_campaign(budget_usd=1.0, spent_usd=0.8, session_budget_usd=0.5)) == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# The loop, driven through the real engine
# ---------------------------------------------------------------------------

class _Coder:
    def __init__(self) -> None:
        self.instructions: list[str] = []

    async def run(self, envelope, emitter, resume_messages=None, ctx=None):
        self.instructions.append(envelope.instruction)
        return AgentResult(task_id=envelope.task_id, agent_id="A", status=AgentStatus.success, cost_usd=0.1,
                           output=AgentOutput(text=f"session notes {len(self.instructions)}"))


def _verdict(passed: bool) -> tuple[Verification, AgentResult]:
    v = Verification(passed=passed, checks=[CriterionCheck(criterion="c", met=passed, evidence="e")])
    return v, AgentResult(task_id="v", agent_id="A.verifier", status=AgentStatus.success, cost_usd=0.01)


@pytest.fixture
def manager(dirs, monkeypatch):
    monkeypatch.setattr("agentflow.config.settings.capture_events", False)
    monkeypatch.setattr("agentflow.config.settings.capture_results", False)
    monkeypatch.setattr("agentflow.config.settings.direct_agent_id", "")
    monkeypatch.setattr("agentflow.config.settings.enable_decomposer", False)
    # One verdict per session: in-run repair rounds are covered by test_verifier.py.
    monkeypatch.setattr("agentflow.config.settings.verification_max_repairs", 0)
    monkeypatch.setattr("agentflow.orchestrator.engine.stream_registry", StreamRegistry())
    monkeypatch.setattr("agentflow.orchestrator.engine.context_store", ContextStore())
    monkeypatch.setattr("agentflow.orchestrator.engine.task_bus", TaskBus())
    registry = AgentRegistry()
    registry.register(AgentManifest(agent_id="A", domain="d", system_prompt="s"))
    eng = OrchestratorEngine(registry)
    eng._generate_run_name = AsyncMock(return_value="n")
    coder = _Coder()
    eng._agent_instances = {"A": coder}
    return CampaignManager(eng), coder


async def _drive(manager: CampaignManager, features: list[Feature], session_verdicts: list,
                 regression_verdicts: list | None = None, **request) -> str:
    campaign = manager.create(CampaignRequest(task="build an app", agent_id="A", **request))
    with (
        patch("agentflow.orchestrator.campaign.define_features", AsyncMock(return_value=features)),
        patch("agentflow.orchestrator.engine.verify_subtask", AsyncMock(side_effect=session_verdicts)),
        patch("agentflow.orchestrator.campaign.verify_subtask",
              AsyncMock(side_effect=regression_verdicts or [_verdict(True)] * 20)),
        patch("agentflow.orchestrator.engine.compile_report", AsyncMock(return_value="report.md")),
    ):
        await manager.start(campaign.campaign_id)
    return campaign.campaign_id


async def test_campaign_runs_until_every_feature_is_verified(manager):
    mgr, coder = manager
    cid = await _drive(mgr, _features(2), [_verdict(True), _verdict(False), _verdict(True)])

    view = mgr.store.view(cid)
    assert view.campaign.status == CampaignStatus.completed
    assert [f.status for f in view.features] == [FeatureStatus.passing, FeatureStatus.passing]
    assert len(view.campaign.sessions) == 3 and view.features[1].attempts == 1
    assert view.campaign.pending_session is None
    # Each session targets one feature; the retry sees its own failed attempt's notes.
    assert 'feature="F01"' in coder.instructions[0] and 'feature="F02"' in coder.instructions[1]
    assert "attempted 1 time" in coder.instructions[2] and "session notes 2" in coder.instructions[2]
    # 3 sessions × (agent 0.1 + verifier 0.01) + 2 regression checks × 0.01
    assert view.campaign.spent_usd == pytest.approx(3 * 0.11 + 2 * 0.01)
    assert view.campaign.workspace == f"campaign-{cid}"


async def test_feature_failing_its_attempts_is_blocked_and_campaign_stops(manager, monkeypatch):
    mgr, _ = manager
    monkeypatch.setattr("agentflow.config.settings.campaign_max_feature_attempts", 2)
    cid = await _drive(mgr, _features(1), [_verdict(False), _verdict(False)])
    view = mgr.store.view(cid)
    assert view.features[0].status == FeatureStatus.blocked
    assert view.campaign.status == CampaignStatus.stopped and "blocked" in view.campaign.stop_reason


async def test_inconclusive_verification_does_not_pass_a_feature(manager):
    mgr, _ = manager
    inconclusive = (Verification(passed=None), AgentResult(task_id="v", agent_id="v", status=AgentStatus.success))
    cid = await _drive(mgr, _features(1), [inconclusive, _verdict(True)])
    view = mgr.store.view(cid)
    assert view.features[0].attempts == 1 and view.features[0].status == FeatureStatus.passing


async def test_max_sessions_and_budget_stop_the_campaign(manager):
    mgr, _ = manager
    cid = await _drive(mgr, _features(3), [_verdict(True)] * 3, max_sessions=1)
    assert mgr.store.view(cid).campaign.stop_reason.startswith("reached max_sessions (1)")

    cid = await _drive(mgr, _features(3), [_verdict(True)] * 3, budget_usd=0.1)
    view = mgr.store.view(cid)
    assert view.campaign.status == CampaignStatus.stopped and "budget" in view.campaign.stop_reason
    assert len(view.campaign.sessions) == 1


async def test_regression_reopens_a_passing_feature(manager):
    mgr, coder = manager
    cid = await _drive(
        mgr, _features(2),
        session_verdicts=[_verdict(True), _verdict(True), _verdict(True)],
        regression_verdicts=[_verdict(False), _verdict(True), _verdict(True)],
    )
    view = mgr.store.view(cid)
    assert view.campaign.status == CampaignStatus.completed
    # Session 2 fixed the regressed F01 before F02 got its turn.
    assert 'feature="F01"' in coder.instructions[1] and "REGRESSION" in coder.instructions[1]
    assert 'feature="F02"' in coder.instructions[2]


async def test_failed_definition_fails_the_campaign(manager):
    mgr, _ = manager
    cid = await _drive(mgr, [], [])
    assert mgr.store.view(cid).campaign.status == CampaignStatus.failed


async def test_pause_request_takes_effect_after_the_current_session(manager):
    mgr, coder = manager
    original = coder.run

    async def run_then_pause(envelope, emitter, resume_messages=None, ctx=None):
        result = await original(envelope, emitter)
        mgr.request_pause(next(iter(mgr._tasks)))
        return result

    coder.run = run_then_pause
    cid = await _drive(mgr, _features(2), [_verdict(True)] * 2)
    view = mgr.store.view(cid)
    assert view.campaign.status == CampaignStatus.paused and len(view.campaign.sessions) == 1
    assert view.features[0].status == FeatureStatus.passing  # the session's outcome was kept


async def test_resume_absorbs_a_session_that_finished_before_the_crash(manager):
    """The process died after the session run completed but before the ledger update."""
    mgr, coder = manager
    store = mgr.store
    store.save(_campaign(status=CampaignStatus.paused, workspace="proj", sessions=["done-run"],
                         pending_session="done-run", pending_feature="F01"))
    store.save_features("c1", _features(1))
    s = RunSession("done-run")
    await s.append(SessionEventType.run_started, data={"task": "t", "workspace": "proj"})
    await s.append(SessionEventType.result_stored, subtask_id="feature", data={"result": AgentResult(
        task_id="t", agent_id="A", status=AgentStatus.success, cost_usd=0.2,
        verification=Verification(passed=True)).model_dump(mode="json")})
    await s.append(SessionEventType.cost_added, data={"cost_usd": 0.2})
    await s.append(SessionEventType.run_finished, data={"status": "complete"})

    with patch("agentflow.orchestrator.engine.compile_report", AsyncMock(return_value="r")):
        await mgr.start("c1")

    view = store.view("c1")
    assert view.features[0].status == FeatureStatus.passing and coder.instructions == []
    assert view.campaign.status == CampaignStatus.completed and view.campaign.spent_usd == pytest.approx(0.2)


async def test_restart_pauses_running_campaigns(manager):
    mgr, _ = manager
    mgr.store.save(_campaign(status=CampaignStatus.running))
    assert await mgr.mark_interrupted() == ["c1"]
    assert mgr.store.load("c1").status == CampaignStatus.paused


async def test_starting_twice_is_refused(manager):
    from agentflow.orchestrator.campaign import CampaignActiveError
    mgr, _ = manager
    mgr.store.save(_campaign(status=CampaignStatus.paused))
    mgr._tasks["c1"] = asyncio.create_task(asyncio.Event().wait())
    try:
        with pytest.raises(CampaignActiveError):
            mgr.start("c1")
    finally:
        mgr._tasks["c1"].cancel()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

async def test_campaign_routes(manager, monkeypatch):
    from agentflow.api import routes
    mgr, _ = manager
    monkeypatch.setattr(routes, "_get_campaigns", lambda: mgr)

    with pytest.raises(HTTPException) as exc:
        await routes.start_campaign(CampaignRequest(task="t", agent_id="Nope"))
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        await routes.get_campaign("missing")
    assert exc.value.status_code == 404

    mgr.store.save(_campaign(status=CampaignStatus.paused))
    with pytest.raises(HTTPException) as exc:
        await routes.pause_campaign("c1")
    assert exc.value.status_code == 409  # not running
    assert (await routes.list_campaigns()).campaigns[0].campaign.campaign_id == "c1"


# ---------------------------------------------------------------------------
# Operator controls: per-feature sessions, limits, retrying blocked features
# ---------------------------------------------------------------------------

async def _resume(manager: CampaignManager, cid: str, session_verdicts: list) -> None:
    """Resume a stopped/paused campaign with fresh scripted verdicts."""
    with (
        patch("agentflow.orchestrator.engine.verify_subtask", AsyncMock(side_effect=session_verdicts)),
        patch("agentflow.orchestrator.campaign.verify_subtask", AsyncMock(side_effect=[_verdict(True)] * 20)),
        patch("agentflow.orchestrator.engine.compile_report", AsyncMock(return_value="report.md")),
    ):
        await manager.start(cid)


async def test_each_feature_records_its_sessions_including_the_one_in_progress(manager):
    mgr, coder = manager
    seen_in_progress: list[tuple[str, list[str], str | None]] = []
    original = coder.run

    async def run_and_peek(envelope, emitter, resume_messages=None, ctx=None):
        cid = next(iter(mgr._tasks))
        c = mgr.store.load(cid)
        feature = next(f for f in mgr.store.features(cid) if f.id == c.pending_feature)
        seen_in_progress.append((feature.id, list(feature.sessions), c.pending_session))
        return await original(envelope, emitter)

    coder.run = run_and_peek
    cid = await _drive(mgr, _features(2), [_verdict(True), _verdict(False), _verdict(True)])

    view = mgr.store.view(cid)
    f1, f2 = view.features
    assert f1.sessions == view.campaign.sessions[:1]
    assert f2.sessions == view.campaign.sessions[1:]  # the failed attempt and the passing one
    # While a session runs, its feature already lists it (the ledger can show it as active).
    assert all(pending in sessions for _, sessions, pending in seen_in_progress)


def test_legacy_ledgers_derive_sessions_from_last_run(dirs):
    store = CampaignStore()
    store.save(_campaign())
    legacy = _features(1)
    legacy[0].last_run_id = "old-run"
    store.save_features("c1", legacy)
    raw = store.dir("c1") / "features.json"
    raw.write_text(raw.read_text().replace('"sessions": [],', ""))  # as written before the field existed
    assert store.features("c1")[0].sessions == ["old-run"]


async def test_resume_at_a_limit_explains_itself_then_raising_it_continues(manager):
    mgr, _ = manager
    cid = await _drive(mgr, _features(2), [_verdict(True)], max_sessions=1)
    stopped = mgr.store.load(cid)
    assert stopped.status == CampaignStatus.stopped
    assert "Raise max_sessions, then resume." in stopped.stop_reason

    # Previously: resume started the loop, which stopped again silently.
    with pytest.raises(CampaignStateError, match="Cannot resume: reached max_sessions"):
        mgr.start(cid)

    mgr.update_limits(cid, CampaignLimitsUpdate(max_sessions=3))
    await _resume(mgr, cid, [_verdict(True)])
    view = mgr.store.view(cid)
    assert view.campaign.status == CampaignStatus.completed and view.campaign.max_sessions == 3


async def test_budget_limit_can_be_raised_or_removed(manager):
    mgr, _ = manager
    cid = await _drive(mgr, _features(3), [_verdict(True)], budget_usd=0.1)
    with pytest.raises(CampaignStateError, match="Raise or remove the budget"):
        mgr.start(cid)

    mgr.update_limits(cid, CampaignLimitsUpdate.model_validate({"budget_usd": None}))  # explicit null
    c = mgr.store.load(cid)
    assert c.budget_usd is None and c.max_sessions == 10  # omitted fields unchanged
    await _resume(mgr, cid, [_verdict(True), _verdict(True)])
    assert mgr.store.load(cid).status == CampaignStatus.completed


def test_limits_validation(manager):
    mgr, _ = manager
    mgr.store.save(_campaign(status=CampaignStatus.stopped))
    with pytest.raises(CampaignStateError, match="cannot be removed"):
        mgr.update_limits("c1", CampaignLimitsUpdate.model_validate({"max_sessions": None}))
    mgr.store.save(_campaign(status=CampaignStatus.completed))
    with pytest.raises(CampaignStateError, match="no longer apply"):
        mgr.update_limits("c1", CampaignLimitsUpdate(max_sessions=5))
    with pytest.raises(ValueError):
        CampaignLimitsUpdate(max_sessions=0)


async def test_blocked_feature_can_be_retried_with_guidance(manager, monkeypatch):
    mgr, coder = manager
    monkeypatch.setattr("agentflow.config.settings.campaign_max_feature_attempts", 2)
    cid = await _drive(mgr, _features(1), [_verdict(False), _verdict(False)])
    assert mgr.store.view(cid).features[0].status == FeatureStatus.blocked
    with pytest.raises(CampaignStateError, match="Retry a blocked feature"):
        mgr.start(cid)

    feature = mgr.retry_feature(cid, "F01", "The checks read README.md, not docs/README.md.")
    assert feature.status == FeatureStatus.failing and feature.attempts == 0

    await _resume(mgr, cid, [_verdict(True)])
    retried_instruction = coder.instructions[-1]
    assert "<operator_guidance>" in retried_instruction and "not docs/README.md" in retried_instruction
    view = mgr.store.view(cid)
    assert view.features[0].status == FeatureStatus.passing
    assert view.features[0].guidance == ""  # cleared once it passed
    assert len(view.features[0].sessions) == 3


def test_only_blocked_or_failing_features_can_be_retried(manager):
    from agentflow.orchestrator.campaign import CampaignNotFoundError
    mgr, _ = manager
    mgr.store.save(_campaign())
    features = _features(2)
    features[0].status = FeatureStatus.passing
    mgr.store.save_features("c1", features)
    with pytest.raises(CampaignStateError, match="only blocked or failing"):
        mgr.retry_feature("c1", "F01")
    with pytest.raises(CampaignStateError):
        mgr.retry_feature("c1", "F02")  # pending
    with pytest.raises(CampaignNotFoundError):
        mgr.retry_feature("c1", "F99")


async def test_operator_routes(manager, monkeypatch):
    from agentflow.api import routes
    from agentflow.core.models import FeatureRetryRequest
    mgr, _ = manager
    monkeypatch.setattr(routes, "_get_campaigns", lambda: mgr)
    mgr.store.save(_campaign(status=CampaignStatus.stopped))
    features = _features(1)
    features[0].status = FeatureStatus.blocked
    mgr.store.save_features("c1", features)

    view = await routes.update_campaign_limits("c1", CampaignLimitsUpdate(max_sessions=20))
    assert view.campaign.max_sessions == 20
    view = await routes.retry_campaign_feature("c1", "F01", FeatureRetryRequest(guidance="try X"))
    assert view.features[0].status == FeatureStatus.failing and view.features[0].guidance == "try X"
    for call, code in (
        (routes.update_campaign_limits("nope", CampaignLimitsUpdate(max_sessions=2)), 404),
        (routes.retry_campaign_feature("c1", "F99", FeatureRetryRequest()), 404),
    ):
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == code
    view = await routes.retry_campaign_feature("c1", "F01", FeatureRetryRequest())  # failing → retryable again
    assert view.features[0].attempts == 0


def test_resume_reports_every_obstacle_at_once(manager):
    """A campaign can be blocked, at its session limit and out of budget together;
    resume names all three so they can be fixed in one go."""
    mgr, _ = manager
    mgr.store.save(_campaign(status=CampaignStatus.stopped, max_sessions=2, sessions=["a", "b"],
                             budget_usd=0.5, spent_usd=0.6))
    features = _features(2)
    features[0].status, features[1].status = FeatureStatus.blocked, FeatureStatus.passing
    mgr.store.save_features("c1", features)
    with pytest.raises(CampaignStateError) as exc:
        mgr.start("c1")
    message = str(exc.value)
    for expected in ("only blocked features remain", "reached max_sessions (2)", "budget $0.50 spent",
                     "Retry a blocked feature", "Raise max_sessions", "Raise or remove the budget"):
        assert expected in message
