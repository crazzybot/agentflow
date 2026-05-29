"""Async HTTP client for the AgentFlow API."""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx


class AgentFlowError(Exception):
    pass


async def check_health(base_url: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{base_url}/health", timeout=5.0)
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        raise AgentFlowError(f"Cannot connect to {base_url} — is the server running?")
    except httpx.HTTPStatusError as e:
        raise AgentFlowError(f"HTTP {e.response.status_code}: {e.response.text}")


async def start_run(base_url: str, task: str, context: dict[str, Any]) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url}/api/run",
                json={"task": task, "context": context},
                timeout=15.0,
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        raise AgentFlowError(f"Cannot connect to {base_url} — is the server running?")
    except httpx.HTTPStatusError as e:
        raise AgentFlowError(f"HTTP {e.response.status_code}: {e.response.text}")


async def stream_events(base_url: str, run_id: str) -> AsyncIterator[dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(None)) as client:
            async with client.stream("GET", f"{base_url}/api/run/{run_id}/stream") as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        try:
                            yield json.loads(line[6:])
                        except json.JSONDecodeError:
                            pass
    except httpx.ConnectError:
        raise AgentFlowError(f"Connection lost to {base_url}")
    except httpx.HTTPStatusError as e:
        raise AgentFlowError(f"HTTP {e.response.status_code}: {e.response.text}")
