from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = ""
    planner_model: str = "claude-sonnet-5-5"
    agent_model: str = "claude-opus-5-5"
    # Cheap model for the small bookkeeping calls (run naming, direct/plan routing).
    reporter_model: str = "claude-haiku-4-5-20251001"

    # Final report synthesis — the user-facing output of every run, so it defaults to
    # the "standard" tier rather than reporter_model. Set REPORT_MODEL to pin a raw
    # model id instead (wins over the tier).
    report_model: str = ""
    report_model_tier: str = "standard"
    report_max_tokens: int = 16_000
    # Reasoning effort for report synthesis; ignored for models without adaptive thinking.
    report_thinking_effort: str = "low"

    # Named cost tiers, resolved by AgentManifest.model_tier / Subtask.model_tier via
    # resolve_model_tier() below. Re-tiering a whole fleet of agents (e.g. swapping which
    # model "premium" points at) is then one env var change instead of editing every
    # manifest's raw `model` field. `standard` intentionally mirrors agent_model's default
    # so leaving model_tier unset and setting model_tier="standard" behave identically.
    model_tier_economy: str = "claude-haiku-4-5-20251001"
    model_tier_standard: str = "claude-opus-5-5"
    model_tier_premium: str = "claude-fable-5-1"

    task_timeout_ms: int = 3_600_000  # 1 hour — budget exhaustion is the real limiter
    task_max_retries: int = 1

    manifests_dir: str = "manifests"
    workspace_dir: str = "workspace"
    runs_dir: str = ".runs"
    skills_dir: str = "skills"
    sandbox_python: str = "sandbox/.venv/bin/python"
    # Extra environment variable names passed through to bash_exec / python_exec
    # beyond the built-in allowlist (PATH, LANG, …). Credentials are withheld by
    # default; add only what a toolchain needs, e.g. SANDBOX_ENV_PASSTHROUGH='["NODE_OPTIONS"]'.
    sandbox_env_passthrough: list[str] = []
    # Where bash_exec / bash_exec_readonly / python_exec run (tools/sandbox.py):
    # "local" (host subprocess, scrubbed env) or "docker" (per-run container with the
    # workspace bind-mounted at /workspace; fails closed if Docker is unavailable).
    sandbox_backend: str = "local"
    sandbox_docker_image: str = "python:3.13-slim"
    sandbox_docker_python: str = "python3"
    # "bridge" keeps package installs working; "none" cuts the sandbox off the network.
    sandbox_docker_network: str = "bridge"
    sandbox_docker_memory: str = "2g"
    sandbox_docker_cpus: float = 2.0
    sandbox_docker_pids_limit: int = 512
    agent_max_iterations: int = 10  # fallback when no budget is set
    agent_max_tokens_fallback: int = 8_192  # max_tokens per call when no budget is set
    agent_max_tokens_cap: int = 32_768  # upper bound on max_tokens derived from budget
    # In-loop retries after a response is cut off by max_tokens. Each retry tells the
    # model its output was truncated and (without a task budget) doubles max_tokens,
    # up to agent_max_tokens_cap. Replaying the identical request would just hit the
    # same wall again.
    agent_max_tokens_retries: int = 2
    # Minimum remaining budget (USD) required to attempt another agent iteration.
    # Below this threshold the agent stops and returns partial rather than starting
    # a call that is almost certain to be cut short by max_tokens.
    agent_min_iteration_budget_usd: float = 0.002

    enable_prompt_caching: bool = True
    capture_events: bool = False
    capture_results: bool = False
    # Resume runs interrupted by a restart automatically at startup (from their
    # session logs). Off by default: resuming spends API budget unprompted.
    # Interrupted runs can always be resumed explicitly via POST /api/runs/{id}/resume.
    resume_interrupted_runs: bool = False

    # Pricing (USD per 1M tokens) — defaults match claude-opus-5-5 (agent_model's
    # default): $4/$20, cache writes 1.25x input, cache reads 0.05x input.
    # These are only the fallback rates for a model not in agents/agent.py's
    # _MODEL_PRICING table — every listed model is priced from that table.
    cost_per_1m_input_tokens: float = 4.0
    cost_per_1m_output_tokens: float = 20.0
    cost_per_1m_cache_write_tokens: float = 5.0
    cost_per_1m_cache_read_tokens: float = 0.20
    # Thinking tokens are billed as output tokens.
    cost_per_1m_thinking_tokens: float = 20.0

    # Max times a partial result triggers a continuation before accepting it
    max_continuations: int = 3

    # Max lines file_read returns in a single call (prevents context flooding)
    file_read_max_lines: int = 200

    # Max chars file_read returns in a single call, as a backstop alongside
    # file_read_max_lines for files with pathologically long lines (minified
    # code, long log lines) that would otherwise blow past a reasonable
    # response size while still under the line cap.
    file_read_max_chars: int = 12_000
    # Read-time context snipping (agents/context_view.py): once an agent's prompt
    # exceeds this many tokens, older tool results are replaced by recall stubs in
    # what the model sees (full history is kept). 0 disables snipping.
    context_snip_trigger_tokens: int = 100_000
    # Most recent tool-result turns always shown in full.
    context_snip_keep_recent: int = 3
    # Tool results shorter than this are left inline (a stub would save little).
    context_snip_min_chars: int = 1_000

    # Max iterations the agentic planner may use for workspace exploration
    planner_max_iterations: int = 15

    # Max iterations for the per-subtask decomposer ReAct loop
    decomposer_max_iterations: int = 10

    # Independent verification of subtasks that carry acceptance criteria
    # (orchestrator/verifier.py). The verifier is read-only and cheap by default.
    enable_verifier: bool = True
    verifier_max_iterations: int = 8
    verifier_model_tier: str = "economy"
    # Repair rounds (agent continues with the verifier's findings) before a still-
    # failing subtask is downgraded to partial.
    verification_max_repairs: int = 1

    # Git checkpoints of each run's workspace in a shadow repo under the run dir
    # (orchestrator/checkpoints.py): baseline + one per finished subtask, and
    # rollback of a failed attempt's files before a retry. Needs `git` on PATH.
    enable_checkpoints: bool = True

    # Campaigns (orchestrator/campaign.py): long-horizon work in fresh-context sessions.
    campaign_max_features: int = 30
    # Sessions a feature may fail verification before it is marked blocked.
    campaign_max_feature_attempts: int = 2
    # Previously passing features re-verified (read-only) before each session.
    campaign_regression_checks: int = 1

    # Set ENABLE_DECOMPOSER=false to skip per-subtask decomposition entirely.
    # Subtasks then execute as the planner produced them, with no extra ReAct loop.
    enable_decomposer: bool = True

    # Agent used when mode="direct" or when auto-classification routes to direct.
    # Must match an agent_id in the manifests directory.
    direct_agent_id: str = ""

    # Global reasoning effort ("low" | "medium" | "high" | "xhigh" | "max") applied to
    # every agent that does not declare its own thinking_effort in its manifest (and
    # isn't given one by the planner). Sent explicitly because the model default differs
    # by model: Sonnet 5 / Opus 5+ think adaptively at "high" when the parameter is
    # omitted, while Sonnet/Opus 4.6 don't think at all. Ignored for models without
    # adaptive thinking (e.g. Haiku 4.5). Set to "" to omit it and use the model default.
    agent_thinking_effort: str = "medium"

    # How long (seconds) the engine waits for human input before timing out and
    # accepting the partial result.  Default: 30 minutes.
    human_input_timeout_s: float = 1800

    tavily_api_key: str = ""

    # State backend: "memory" (default) or "redis"
    # Set STATE_BACKEND=redis to enable Redis-backed state.
    state_backend: str = "memory"
    redis_url: str = "redis://localhost:6379"
    # TTL (seconds) applied to all run-scoped Redis keys.  Default: 24 hours.
    redis_key_ttl: int = 86_400
    # Maximum connections in the shared Redis pool.
    redis_max_connections: int = 50
    # Per-run ownership lease (Redis backend): the owning replica renews it every
    # ttl/3 seconds. A crashed owner's runs become reconcilable/resumable once it
    # lapses; a cross-replica cancel takes effect within ~ttl/3 seconds.
    run_lease_ttl_s: float = 15.0

    # Where subtask agents execute (orchestrator/workers.py): "local" (in this
    # process) or "workers" (queued to `agentflow worker` processes; requires
    # STATE_BACKEND=redis and a runs_dir/workspace shared with the workers).
    dispatch_mode: str = "local"
    # Fail a queued invocation no worker has claimed after this long.
    worker_claim_timeout_s: float = 60.0
    # Workers refresh their claim this often; 3 missed beats = worker presumed dead.
    worker_heartbeat_s: float = 5.0

    def resolve_model_tier(self, tier: str | None) -> str | None:
        """Resolve a named cost tier to a model id, or None if *tier* is unset/unrecognised.

        Callers treat None as "no override" and fall back to their own default —
        an unrecognised tier name (e.g. a typo in a manifest or a planner-emitted
        value) is intentionally non-fatal for the same reason.
        """
        return {
            "economy": self.model_tier_economy,
            "standard": self.model_tier_standard,
            "premium": self.model_tier_premium,
        }.get(tier or "")


settings = Settings()
