from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = ""
    planner_model: str = "claude-sonnet-4-6"
    agent_model: str = "claude-sonnet-4-6"
    reporter_model: str = "claude-haiku-4-5-20251001"

    task_timeout_ms: int = 300_000
    task_max_retries: int = 1
    task_max_tokens: int = 4096

    manifests_dir: str = "manifests"
    workspace_dir: str = "workspace"
    skills_dir: str = "skills"
    sandbox_python: str = "sandbox/.venv/bin/python"
    agent_max_iterations: int = 10

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


settings = Settings()
