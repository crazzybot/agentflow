"""Prompt-cache-aware wrapper around anthropic.AsyncAnthropic.

Prompt caching is applied automatically to the system prompt and tool list
on every call (when settings.enable_prompt_caching is True).  Anthropic
charges 25 % to write a cache entry and only 10 % to read it back, so any
repeated system prompt or tool set that exceeds the 1 024-token minimum will
reduce cost on every subsequent call within the 5-minute TTL window.

Rate limiting is handled by the Anthropic SDK (max_retries=4, exponential
backoff on 429/500).  A proactive per-process limiter would not coordinate
across replicas and would need to be reconfigured per account tier anyway.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

_MAX_RETRIES = 4


# ---------------------------------------------------------------------------
# Usage stats
# ---------------------------------------------------------------------------

@dataclass
class UsageStats:
    """Cumulative token usage and prompt-cache metrics across all API calls."""
    total_requests: int = 0
    total_input_tokens: int = 0      # non-cached input tokens billed at full rate
    total_output_tokens: int = 0     # includes thinking tokens when API bundles them
    total_thinking_tokens: int = 0   # extended thinking tokens (subset of output_tokens)
    cache_creation_tokens: int = 0   # tokens written to the cache (25 % cost)
    cache_read_tokens: int = 0       # tokens served from cache (10 % cost)

    @property
    def cache_hit_rate(self) -> float:
        denom = self.total_input_tokens + self.cache_read_tokens
        return self.cache_read_tokens / denom if denom else 0.0

    def log_summary(self) -> None:
        logger.info(
            "LLM usage — requests=%d in=%d out=%d thinking=%d "
            "cache_written=%d cache_read=%d hit_rate=%.1f%%",
            self.total_requests,
            self.total_input_tokens,
            self.total_output_tokens,
            self.total_thinking_tokens,
            self.cache_creation_tokens,
            self.cache_read_tokens,
            self.cache_hit_rate * 100,
        )


# ---------------------------------------------------------------------------
# Prompt caching helpers
# ---------------------------------------------------------------------------

def _apply_caching(
    system: str | list | None,
    tools: list[dict] | None,
) -> tuple[list[dict] | None, list[dict] | None]:
    """Return (cached_system, cached_tools) with cache_control breakpoints injected.

    Anthropic caches all content up to and including the block that carries
    cache_control, so placing it on the last system block and the last tool
    definition covers the full prefix in a single breakpoint each.

    Minimum cacheable size is 1 024 tokens; Anthropic silently skips caching
    for smaller content, so adding cache_control is always safe.
    """
    cached_system: list[dict] | None = None
    if isinstance(system, str) and system:
        cached_system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    elif isinstance(system, list) and system:
        cached_system = list(system)
        # If the caller pre-marked a specific block (e.g. to leave a trailing date
        # block uncached), respect that and don't add a second breakpoint.
        already_marked = any(
            isinstance(b, dict) and b.get("cache_control") for b in cached_system
        )
        if not already_marked:
            last = dict(cached_system[-1])
            last["cache_control"] = {"type": "ephemeral"}
            cached_system[-1] = last

    cached_tools: list[dict] | None = None
    if tools:
        cached_tools = list(tools)
        last_tool = dict(cached_tools[-1])
        last_tool.setdefault("cache_control", {"type": "ephemeral"})
        cached_tools[-1] = last_tool

    return cached_system, cached_tools


# ---------------------------------------------------------------------------
# Messages proxy (the public surface that callers use)
# ---------------------------------------------------------------------------

class _MessagesProxy:
    def __init__(
        self,
        inner: anthropic.AsyncAnthropic,
        stats: UsageStats,
        enable_caching: bool,
    ) -> None:
        self._inner = inner
        self._stats = stats
        self._enable_caching = enable_caching

    async def create(self, **kwargs: Any) -> anthropic.types.Message:
        if self._enable_caching:
            system = kwargs.pop("system", None)
            tools = kwargs.pop("tools", None)
            cached_system, cached_tools = _apply_caching(system, tools)
            if cached_system is not None:
                kwargs["system"] = cached_system
            if cached_tools is not None:
                kwargs["tools"] = cached_tools

        betas = kwargs.pop("betas", None)
        if betas:
            response = await self._inner.beta.messages.create(betas=betas, **kwargs)
        else:
            response = await self._inner.messages.create(**kwargs)

        usage = response.usage
        input_tokens: int = usage.input_tokens
        output_tokens: int = usage.output_tokens
        thinking_tokens: int = getattr(usage, "thinking_tokens", 0) or sum(
            len(getattr(block, "thinking", "")) // 4
            for block in response.content
            if getattr(block, "type", "") == "thinking"
        )
        cache_creation: int = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read: int = getattr(usage, "cache_read_input_tokens", 0) or 0

        self._stats.total_requests += 1
        self._stats.total_input_tokens += input_tokens
        self._stats.total_output_tokens += output_tokens
        self._stats.total_thinking_tokens += thinking_tokens
        self._stats.cache_creation_tokens += cache_creation
        self._stats.cache_read_tokens += cache_read

        logger.debug(
            "model=%s in=%d out=%d thinking=%d cache_read=%d cache_create=%d",
            kwargs.get("model", ""), input_tokens, output_tokens, thinking_tokens, cache_read, cache_creation,
        )

        return response


# ---------------------------------------------------------------------------
# Public client
# ---------------------------------------------------------------------------

class LLMClient:
    """Drop-in replacement for anthropic.AsyncAnthropic.

    Wraps ``client.messages.create()`` to inject prompt-cache breakpoints and
    track token usage.  The underlying SDK retries 429/500 responses with
    exponential backoff (max_retries=4).

    Usage::

        client = LLMClient(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            system="You are ...",
            messages=[...],
            max_tokens=1024,
        )
        client.stats.log_summary()
    """

    def __init__(self, api_key: str, enable_prompt_caching: bool = True) -> None:
        self._inner = anthropic.AsyncAnthropic(api_key=api_key, max_retries=_MAX_RETRIES)
        self.stats = UsageStats()
        self.messages = _MessagesProxy(
            self._inner,
            self.stats,
            enable_caching=enable_prompt_caching,
        )
