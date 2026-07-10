from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = ""
    planner_model: str = "claude-sonnet-4-6"
    agent_model: str = "claude-sonnet-4-6"
    reporter_model: str = "claude-haiku-4-5-20251001"

    task_timeout_ms: int = 3_600_000  # 1 hour — budget exhaustion is the real limiter
    task_max_retries: int = 1

    manifests_dir: str = "manifests"
    workspace_dir: str = "workspace"
    runs_dir: str = ".runs"
    skills_dir: str = "skills"
    sandbox_python: str = "sandbox/.venv/bin/python"
    agent_max_iterations: int = 10  # fallback when no budget is set
    agent_max_tokens_fallback: int = 8_192  # max_tokens per call when no budget is set
    # Minimum remaining budget (USD) required to attempt another agent iteration.
    # Below this threshold the agent stops and returns partial rather than starting
    # a call that is almost certain to be cut short by max_tokens.
    agent_min_iteration_budget_usd: float = 0.002

    enable_prompt_caching: bool = True
    capture_events: bool = False
    capture_results: bool = False

    # Pricing (USD per 1M tokens) — defaults match claude-sonnet-4-6
    cost_per_1m_input_tokens: float = 3.0
    cost_per_1m_output_tokens: float = 15.0
    cost_per_1m_cache_write_tokens: float = 3.75
    cost_per_1m_cache_read_tokens: float = 0.30

    # Max times a partial result triggers a continuation before accepting it
    max_continuations: int = 3

    # Max lines file_read returns in a single call (prevents context flooding)
    file_read_max_lines: int = 200

    # Max iterations the agentic planner may use for workspace exploration
    planner_max_iterations: int = 15

    # Max iterations for the per-subtask decomposer ReAct loop
    decomposer_max_iterations: int = 5

    # How long (seconds) the engine waits for human input before timing out and
    # accepting the partial result.  Default: 30 minutes.
    human_input_timeout_s: float = 1800

    # State backend: "memory" (default) or "redis"
    # Set STATE_BACKEND=redis to enable Redis-backed state.
    state_backend: str = "memory"
    redis_url: str = "redis://localhost:6379"
    # TTL (seconds) applied to all run-scoped Redis keys.  Default: 24 hours.
    redis_key_ttl: int = 86_400
    # Maximum connections in the shared Redis pool.
    redis_max_connections: int = 50


settings = Settings()
