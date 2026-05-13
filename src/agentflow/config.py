from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = ""
    planner_model: str = "claude-sonnet-4-6"
    agent_model: str = "claude-sonnet-4-6"

    task_timeout_ms: int = 30_000
    task_max_retries: int = 3
    task_max_tokens: int = 4096

    manifests_dir: str = "manifests"
    workspace_dir: str = "workspace"
    agent_max_iterations: int = 10


settings = Settings()
