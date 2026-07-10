"""Generic Agent — driven entirely by its AgentManifest.

Behaviour is determined by:
  - manifest.system_prompt  — the agent's persona and instructions
  - manifest.tools          — which built-in tools are accessible
  - manifest.mcp_servers    — remote MCP servers to connect and pull tools from

The agentic loop runs until Claude says end_turn or the iteration limit is reached.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import anthropic
from anthropic.types import TextBlock

from agentflow.config import settings
from agentflow.core.models import AgentManifest, AgentOutput, AgentResult, AgentStatus, SSEEventType, TaskEnvelope
from agentflow.llm import LLMClient
from agentflow.tools import tool_registry
from agentflow.tools.mcp_tools import mcp_session

if TYPE_CHECKING:
    from agentflow.orchestrator.stream import StreamEmitter

logger = logging.getLogger(__name__)

# Web pages and large tool outputs can be arbitrarily long. Capping them here
# prevents a single fetch_url call from dominating every subsequent loop iteration
# (the full messages array is re-sent on each turn).
_MAX_TOOL_RESULT_CHARS = 8_000


def _with_message_cache_breakpoint(messages: list[dict]) -> list[dict]:
    """Return messages with cache_control on the last user message's last content block.

    Marks the accumulated conversation history as cacheable so that on each
    subsequent turn only the newest assistant response and user turn are billed
    at full input rate.  This uses one of the two remaining Anthropic cache
    breakpoints (system and tools consume the other two).
    """
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") != "user":
            continue
        content = messages[i].get("content")
        if isinstance(content, str):
            new_content: list = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
        elif isinstance(content, list) and content:
            new_content = list(content)
            last_block = dict(new_content[-1])
            last_block.setdefault("cache_control", {"type": "ephemeral"})
            new_content[-1] = last_block
        else:
            return messages
        out = list(messages)
        out[i] = {**messages[i], "content": new_content}
        return out
    return messages


def _budget_to_max_tokens(remaining_budget: float, last_input_tokens: int) -> int:
    """Compute how many output tokens we can afford with *remaining_budget*.

    We subtract an estimate of the next call's input cost (based on the previous
    call's input token count) to leave room for it, then convert the remainder to
    output tokens using the configured output token price.
    """
    estimated_input_cost = last_input_tokens * settings.cost_per_1m_input_tokens / 1_000_000
    output_budget = remaining_budget - estimated_input_cost
    if output_budget <= 0:
        return 256  # minimum to at least elicit an end_turn
    tokens = int(output_budget / (settings.cost_per_1m_output_tokens / 1_000_000))
    return max(256, min(16_384, tokens))



class Agent:
    """Stateless, manifest-driven agent.  One class handles all agent types."""

    def __init__(self, manifest: AgentManifest, client: LLMClient | anthropic.AsyncAnthropic) -> None:
        self.manifest = manifest
        self.client = client

    @property
    def agent_id(self) -> str:
        return self.manifest.agent_id

    # ------------------------------------------------------------------
    # Public entry point (called by the orchestration engine)
    # ------------------------------------------------------------------

    async def run(
        self,
        envelope: TaskEnvelope,
        emitter: "StreamEmitter",
        resume_messages: list | None = None,
        ctx: Any | None = None,
    ) -> AgentResult:
        start_ms = int(time.time() * 1000)
        emitter.emit(
            SSEEventType.agent_progress,
            agent_id=self.agent_id,
            message=f"Starting: {envelope.instruction[:80]}",
        )
        try:
            result = await self._execute(envelope, emitter, resume_messages=resume_messages, ctx=ctx)
        except Exception as exc:
            logger.exception("[%s] Agent %s raised an unhandled error", envelope.parent_run_id, self.agent_id)
            result = AgentResult(
                task_id=envelope.task_id,
                agent_id=self.agent_id,
                status=AgentStatus.failed,
                error=str(exc),
            )
        result.duration_ms = int(time.time() * 1000) - start_ms
        return result

    # ------------------------------------------------------------------
    # Core execution: build tools, open MCP sessions, run agentic loop
    # ------------------------------------------------------------------

    async def _execute(
        self,
        envelope: TaskEnvelope,
        emitter: "StreamEmitter",
        resume_messages: list | None = None,
        ctx: Any | None = None,
    ) -> AgentResult:
        from agentflow.core.skill_loader import skill_loader

        async with AsyncExitStack() as stack:
            # 1. Gather tool definitions from the global registry (filtered by manifest)
            local_tools = tool_registry.get_many(self.manifest.tools)

            # 2. Connect to each MCP server and collect their tools
            mcp_tools: list = []
            for server_config in self.manifest.mcp_servers:
                server_tool_defs = await stack.enter_async_context(mcp_session(server_config))
                mcp_tools.extend(server_tool_defs)

            all_tools = local_tools + mcp_tools

            # 3. Build a 2-block system prompt list:
            #    Block 0 — static content (persona + full skill docs) → cache_control marks
            #              this for caching; Anthropic serves it from cache on every turn.
            #    Block 1 — current date/time (no cache_control) → always processed fresh
            #              so the model never assumes a stale date.
            static_content = self.manifest.system_prompt
            if self.manifest.skills:
                static_content += skill_loader.full_content(self.manifest.skills)

            utc_now = datetime.now(timezone.utc)
            local_now = utc_now.astimezone()
            now_str = (
                f"{utc_now.strftime('%Y-%m-%d %H:%M UTC')} "
                f"/ {local_now.strftime('%Y-%m-%d %H:%M %Z')}"
            )
            system_blocks = [
                {"type": "text", "text": static_content, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": f"Current date and time: {now_str}"},
            ]

            # 4. Run the agentic loop
            return await self._agentic_loop(envelope, all_tools, system_blocks, emitter, resume_messages=resume_messages, ctx=ctx)

    # ------------------------------------------------------------------
    # Agentic loop with tool execution
    # ------------------------------------------------------------------

    async def _agentic_loop(
        self,
        envelope: TaskEnvelope,
        tools: list,
        system_prompt: str | list,
        emitter: "StreamEmitter",
        resume_messages: list | None = None,
        ctx: Any | None = None,
    ) -> AgentResult:

        if resume_messages is not None:
            # Fix 3: resume a partial run — continue from the existing message thread.
            messages: list[dict[str, Any]] = list(resume_messages)
            # If the last message is from the assistant the model paused mid-thought;
            # add a user prompt to continue.  If it is a user message (tool results)
            # the model will naturally process them on the next iteration.
            if messages and messages[-1].get("role") == "assistant":
                messages.append({
                    "role": "user",
                    "content": "You reached the iteration limit. Continue your work from where you left off — do not repeat completed steps.",
                })
        elif envelope.context.prior_messages:
            # Fix 2: single-dependency chain — inherit the prior subtask's full
            # conversation so the agent already has all file contents in context.
            messages = list(envelope.context.prior_messages)
            user_content = envelope.instruction
            messages.append({"role": "user", "content": user_content})
        else:
            # Standard path: build the initial user message with optional text context.
            user_content = envelope.instruction
            if envelope.context.user_context:
                user_content += (
                    "\n\n<user_context>\n"
                    + json.dumps(envelope.context.user_context, separators=(",", ":"))
                    + "\n</user_context>"
                )
            if envelope.context.prior_results:
                user_content += (
                    "\n\n<context>\n"
                    + json.dumps(envelope.context.prior_results, separators=(",", ":"))
                    + "\n</context>"
                )
            messages = [{"role": "user", "content": user_content}]
        anthropic_tools = [t.to_anthropic_param() for t in tools]
        total_input_tokens = 0
        total_output_tokens = 0
        total_cache_creation_tokens = 0
        total_cache_read_tokens = 0
        total_cost_usd = 0.0
        final_text = ""
        hit_limit = False

        task_budget = envelope.constraints.budget_usd
        max_iterations = self.manifest.max_iterations or settings.agent_max_iterations
        last_input_tokens = 0
        iteration = 0

        while True:
            # --- Determine max_tokens for this iteration ---
            if task_budget is not None:
                remaining = task_budget - total_cost_usd
                if remaining <= settings.agent_min_iteration_budget_usd:
                    hit_limit = True
                    logger.warning(
                        "[%s] Task budget $%.4f exhausted after %d iteration(s) — returning partial result",
                        self.agent_id, task_budget, iteration,
                    )
                    break
                max_tokens = _budget_to_max_tokens(remaining, last_input_tokens)
            else:
                if iteration >= max_iterations:
                    hit_limit = True
                    logger.warning(
                        "[%s] Hit max iterations (%d) — returning partial result",
                        self.agent_id, max_iterations,
                    )
                    break
                max_tokens = settings.agent_max_tokens_fallback

            create_kwargs: dict[str, Any] = {
                "model": settings.agent_model,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": _with_message_cache_breakpoint(messages),
            }
            if anthropic_tools:
                create_kwargs["tools"] = anthropic_tools

            try:
                response = await self.client.messages.create(**create_kwargs)
            except anthropic.BadRequestError as exc:
                body = exc.body if isinstance(exc.body, dict) else {}
                api_message = body.get("error", {}).get("message", "") or str(exc)
                if "content filter" in api_message.lower() or "output blocked" in api_message.lower():
                    logger.error("[%s] Content filter blocked the model output: %s", self.agent_id, exc)
                    return AgentResult(
                        task_id=envelope.task_id,
                        agent_id=self.agent_id,
                        status=AgentStatus.failed,
                        error=f"Content filter blocked response: {api_message}",
                    )
                raise
            u = response.usage
            call_cache_write = u.cache_creation_input_tokens or 0
            call_cache_read = u.cache_read_input_tokens or 0
            last_input_tokens = u.input_tokens
            total_input_tokens += u.input_tokens
            total_output_tokens += u.output_tokens
            total_cache_creation_tokens += call_cache_write
            total_cache_read_tokens += call_cache_read
            total_cost_usd += (
                u.input_tokens * settings.cost_per_1m_input_tokens
                + u.output_tokens * settings.cost_per_1m_output_tokens
                + call_cache_write * settings.cost_per_1m_cache_write_tokens
                + call_cache_read * settings.cost_per_1m_cache_read_tokens
            ) / 1_000_000
            iteration += 1

            # Append the assistant's full response (preserves tool_use blocks)
            messages.append({"role": "assistant", "content": response.content})

            # Collect any text the model produced this turn
            for block in response.content:
                if isinstance(block, TextBlock):
                    final_text = block.text

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason != "tool_use":
                logger.warning(
                    "[%s] Unexpected stop_reason %r at iteration %d",
                    self.agent_id, response.stop_reason, iteration,
                )
                if response.stop_reason == "max_tokens":
                    hit_limit = True
                break

            # Emit any text the model produced alongside tool calls as a thought event
            for block in response.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    emitter.emit(
                        SSEEventType.agent_thought,
                        agent_id=self.agent_id,
                        message=block.text,
                    )

            # Execute all tool calls concurrently, then feed results back
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            tool_results = await asyncio.gather(
                *[self._call_tool(b, tools, emitter) for b in tool_use_blocks]
            )

            messages.append({"role": "user", "content": list(tool_results)})

            # Inject any pending user message as an additional user turn before
            # the next API call, so the model sees it without a separate round-trip.
            if ctx is not None:
                pending = await ctx.pop_user_message(self.agent_id)
                if pending is not None:
                    messages.append({"role": "user", "content": pending})
                    emitter.emit(SSEEventType.run_message_received, agent_id=self.agent_id, message=pending[:120])

        # Try to parse final text as JSON (many system prompts ask for JSON output)
        structured: dict[str, Any] = {}
        try:
            structured = json.loads(final_text)
        except (json.JSONDecodeError, TypeError):
            pass

        return AgentResult(
            task_id=envelope.task_id,
            agent_id=self.agent_id,
            status=AgentStatus.partial if hit_limit else AgentStatus.success,
            output=AgentOutput(structured=structured, text=final_text),
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cache_creation_tokens=total_cache_creation_tokens,
            cache_read_tokens=total_cache_read_tokens,
            tokens_used=total_input_tokens + total_output_tokens + total_cache_creation_tokens + total_cache_read_tokens,
            cost_usd=total_cost_usd,
            messages=messages,
        )

    # ------------------------------------------------------------------
    # Single tool call execution
    # ------------------------------------------------------------------

    async def _call_tool(
        self,
        block: Any,
        tools: list,
        emitter: "StreamEmitter",
    ) -> dict[str, Any]:
        emitter.emit(
            SSEEventType.agent_progress,
            agent_id=self.agent_id,
            message=f"Calling tool: {block.name}",
            data={"tool": block.name, "input": block.input},
        )

        # Find in the tools list for this invocation (built-in + MCP)
        tool_def = next((t for t in tools if t.name == block.name), None)

        if tool_def is not None:
            result_text = await tool_registry.execute(block.name, block.input) \
                if block.name in {t.name for t in tool_registry.all()} \
                else await tool_def.handler(**block.input)
        else:
            result_text = f"Tool {block.name!r} is not available for this agent."

        if len(result_text) > _MAX_TOOL_RESULT_CHARS:
            result_text = result_text[:_MAX_TOOL_RESULT_CHARS] + "\n… [truncated]"

        logger.debug("[%s] Tool %r → %s…", self.agent_id, block.name, result_text[:80])
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": result_text,
        }
