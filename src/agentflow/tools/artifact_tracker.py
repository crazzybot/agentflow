"""Per-run artifact tracking for agent file writes."""
from __future__ import annotations

import asyncio
import json
import uuid
from contextvars import ContextVar
from pathlib import Path


class ArtifactSink:
    """Appends artifact records to artifacts.jsonl as agents write files.

    Thread-safe via asyncio lock; deduplicates by relative path so that
    repeated writes to the same file produce one artifact entry.
    """

    def __init__(self, artifacts_file: Path) -> None:
        self._file = artifacts_file
        self._lock = asyncio.Lock()
        self._seen: set[str] = set()

    async def record(self, rel_path: str) -> None:
        async with self._lock:
            if rel_path in self._seen:
                return
            self._seen.add(rel_path)
            artifact = {
                "id": str(uuid.uuid4()),
                "name": Path(rel_path).name,
                "path": rel_path,
            }
            with self._file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(artifact) + "\n")


# Set to an ArtifactSink at the start of each run; reset when the run ends.
_current_sink: ContextVar[ArtifactSink | None] = ContextVar("_artifact_sink", default=None)
