"""Rate-limited, prompt-cache-aware wrapper around anthropic.AsyncAnthropic.

Rate limits enforced (claude-sonnet-4-6 Tier-1 defaults):
  - 50 requests per minute
  - 30 000 non-cached input tokens per minute
  - 8 000 output tokens per minute

Prompt caching is applied automatically to the system prompt and tool list
on every call (when settings.enable_prompt_caching is True).  Anthropic
charges 25 % to write a cache entry and only 10 % to read it back, so any
repeated system prompt or tool set that exceeds the 1 024-token minimum will
reduce cost on every subsequent call within the 5-minute TTL window.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-model rate-limit table (Anthropic Tier-1 defaults as of 2025)
# ---------------------------------------------------------------------------

_DEFAULT_LIMITS: dict[str, int] = {
    "requests_per_minute": 50,
    "input_tokens_per_minute": 30_000,   # excludes cached tokens
    "output_tokens_per_minute": 8_000,
}

_MODEL_LIMITS: dict[str, dict[str, int]] = {
    "claude-sonnet-4-6": _DEFAULT_LIMITS,
    "claude-haiku-4-5-20251001": {
        "requests_per_minute": 50,
        "input_tokens_per_minute": 30_000,
        "output_tokens_per_minute": 8_000,
    },
}


# ---------------------------------------------------------------------------
# Usage stats
# ---------------------------------------------------------------------------

@dataclass
class UsageStats:
    """Cumulative token usage and prompt-cache metrics across all API calls."""
    total_requests: int = 0
    total_input_tokens: int = 0      # non-cached input tokens billed at full rate
    total_output_tokens: int = 0
    cache_creation_tokens: int = 0   # tokens written to the cache (25 % cost)
    cache_read_tokens: int = 0       # tokens served from cache (10 % cost)

    @property
    def cache_hit_rate(self) -> float:
        denom = self.total_input_tokens + self.cache_read_tokens
        return self.cache_read_tokens / denom if denom else 0.0

    def log_summary(self) -> None:
        logger.info(
            "LLM usage — requests=%d in=%d out=%d "
            "cache_written=%d cache_read=%d hit_rate=%.1f%%",
            self.total_requests,
            self.total_input_tokens,
            self.total_output_tokens,
            self.cache_creation_tokens,
            self.cache_read_tokens,
            self.cache_hit_rate * 100,
        )


# ---------------------------------------------------------------------------
# Sliding-window rate limiter
# ---------------------------------------------------------------------------

@dataclass
class _WindowEntry:
    ts: float
    input_tokens: int = 0
    output_tokens: int = 0


class RateLimiter:
    """Sliding-window rate limiter covering requests, input-token, and output-token budgets.

    In-flight requests are counted against the request limit immediately so
    that concurrent coroutines do not all race through when the window is
    nearly full.  Token budgets are updated after each response because token
    counts are only known once the API returns.
    """

    def __init__(self, rpm: int, itpm: int, otpm: int) -> None:
        self._rpm = rpm
        self._itpm = itpm
        self._otpm = otpm
        self._window: deque[_WindowEntry] = deque()
        self._lock = asyncio.Lock()
        self._in_flight = 0

    def _prune(self) -> None:
        cutoff = time.monotonic() - 60.0
        while self._window and self._window[0].ts < cutoff:
            self._window.popleft()

    def _totals(self) -> tuple[int, int, int]:
        reqs = len(self._window) + self._in_flight
        it = sum(e.input_tokens for e in self._window)
        ot = sum(e.output_tokens for e in self._window)
        return reqs, it, ot

    async def acquire(self) -> None:
        """Block until there is budget for one more request."""
        while True:
            async with self._lock:
                self._prune()
                reqs, it, ot = self._totals()
                if reqs < self._rpm and it < self._itpm and ot < self._otpm:
                    self._in_flight += 1
                    return

                # Calculate how long until the oldest entry falls out of the window
                wait_s = (self._window[0].ts + 60.0 - time.monotonic()) if self._window else 0.5

            wait_s = max(0.25, wait_s)
            logger.warning("Rate limit reached — waiting %.1fs", wait_s)
            await asyncio.sleep(min(wait_s, 5.0))

    def record(self, input_tokens: int, output_tokens: int) -> None:
        """Record actual token usage after a completed API call."""
        self._window.append(_WindowEntry(
            ts=time.monotonic(),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ))
        self._in_flight = max(0, self._in_flight - 1)

    def release_failed(self) -> None:
        """Release the in-flight slot on API error (no tokens to record)."""
        self._in_flight = max(0, self._in_flight - 1)


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
        last = dict(cached_system[-1])
        last.setdefault("cache_control", {"type": "ephemeral"})
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
        limiters: dict[str, RateLimiter],
        stats: UsageStats,
        enable_caching: bool,
    ) -> None:
        self._inner = inner
        self._limiters = limiters
        self._stats = stats
        self._enable_caching = enable_caching

    def _limiter_for(self, model: str) -> RateLimiter:
        if model not in self._limiters:
            limits = _MODEL_LIMITS.get(model, _DEFAULT_LIMITS)
            self._limiters[model] = RateLimiter(
                rpm=limits["requests_per_minute"],
                itpm=limits["input_tokens_per_minute"],
                otpm=limits["output_tokens_per_minute"],
            )
        return self._limiters[model]

    async def create(self, **kwargs: Any) -> anthropic.types.Message:
        model: str = kwargs.get("model", "")
        limiter = self._limiter_for(model)

        if self._enable_caching:
            system = kwargs.pop("system", None)
            tools = kwargs.pop("tools", None)
            cached_system, cached_tools = _apply_caching(system, tools)
            if cached_system is not None:
                kwargs["system"] = cached_system
            if cached_tools is not None:
                kwargs["tools"] = cached_tools

        await limiter.acquire()
        try:
            response = await self._inner.messages.create(**kwargs)
        except Exception:
            limiter.release_failed()
            raise

        usage = response.usage
        input_tokens: int = usage.input_tokens
        output_tokens: int = usage.output_tokens
        cache_creation: int = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read: int = getattr(usage, "cache_read_input_tokens", 0) or 0

        limiter.record(input_tokens, output_tokens)

        self._stats.total_requests += 1
        self._stats.total_input_tokens += input_tokens
        self._stats.total_output_tokens += output_tokens
        self._stats.cache_creation_tokens += cache_creation
        self._stats.cache_read_tokens += cache_read

        logger.debug(
            "model=%s in=%d out=%d cache_read=%d cache_create=%d",
            model, input_tokens, output_tokens, cache_read, cache_creation,
        )

        return response


# ---------------------------------------------------------------------------
# Public client
# ---------------------------------------------------------------------------

class LLMClient:
    """Drop-in replacement for anthropic.AsyncAnthropic.

    Wraps ``client.messages.create()`` to enforce per-model sliding-window
    rate limits and inject prompt-cache breakpoints automatically.

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
        self._inner = anthropic.AsyncAnthropic(api_key=api_key)
        self._limiters: dict[str, RateLimiter] = {}
        self.stats = UsageStats()
        self.messages = _MessagesProxy(
            self._inner,
            self._limiters,
            self.stats,
            enable_caching=enable_prompt_caching,
        )
