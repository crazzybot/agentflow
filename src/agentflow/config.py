from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = ""
    planner_model: str = "claude-sonnet-5"
    agent_model: str = "claude-sonnet-5"
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
    model_tier_standard: str = "claude-sonnet-5"
    model_tier_premium: str = "claude-opus-4-8"

    task_timeout_ms: int = 3_600_000  # 1 hour — budget exhaustion is the real limiter
    task_max_retries: int = 1

    manifests_dir: str = "manifests"
    workspace_dir: str = "workspace"
    runs_dir: str = ".runs"
    skills_dir: str = "skills"
    sandbox_python: str = "sandbox/.venv/bin/python"
    agent_max_iterations: int = 10  # fallback when no budget is set
    agent_max_tokens_fallback: int = 8_192  # max_tokens per call when no budget is set
    agent_max_tokens_cap: int = 32_768  # upper bound on max_tokens derived from budget
    # Minimum remaining budget (USD) required to attempt another agent iteration.
    # Below this threshold the agent stops and returns partial rather than starting
    # a call that is almost certain to be cut short by max_tokens.
    agent_min_iteration_budget_usd: float = 0.002

    enable_prompt_caching: bool = True
    capture_events: bool = False
    capture_results: bool = False

    # Pricing (USD per 1M tokens) — defaults match claude-sonnet-5's standard rate
    # ($2/$10; the launch "introductory" rate became permanent on 2026-09-01).
    # These are only the fallback rates for a model not in agents/agent.py's
    # _MODEL_PRICING table — every listed model is priced from that table.
    cost_per_1m_input_tokens: float = 2.0
    cost_per_1m_output_tokens: float = 10.0
    cost_per_1m_cache_write_tokens: float = 2.50
    cost_per_1m_cache_read_tokens: float = 0.20
    # Thinking tokens are billed as output tokens.
    cost_per_1m_thinking_tokens: float = 10.0

    # Max times a partial result triggers a continuation before accepting it
    max_continuations: int = 3

    # Max lines file_read returns in a single call (prevents context flooding)
    file_read_max_lines: int = 200

    # Max chars file_read returns in a single call, as a backstop alongside
    # file_read_max_lines for files with pathologically long lines (minified
    # code, long log lines) that would otherwise blow past a reasonable
    # response size while still under the line cap.
    file_read_max_chars: int = 12_000

    # Max iterations the agentic planner may use for workspace exploration
    planner_max_iterations: int = 15

    # Max iterations for the per-subtask decomposer ReAct loop
    decomposer_max_iterations: int = 10

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
