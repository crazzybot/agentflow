"""ContextVar-based KB dispatch hook wired by the orchestrator at run start.

OrchestratorEngine.run() sets _kb_dispatch_fn to an async callable that accepts
a plain-text instruction and dispatches a KnowledgebaseAgent subtask within the
active run.  Built-in tools (e.g. download_document) read this var to trigger KB
ingest without needing a direct reference to the engine or RunContext.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar

# Callable signature: (instruction: str) -> str
# Set to None when no run is active or KnowledgebaseAgent is not configured.
_kb_dispatch_fn: ContextVar[Callable[[str], Awaitable[str]] | None] = ContextVar(
    "_kb_dispatch_fn", default=None
)
