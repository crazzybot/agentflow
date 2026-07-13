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


def _to_dict_content(content: list) -> list[dict]:
    """Convert Anthropic SDK response blocks to plain dicts for message storage.

    Storing plain dicts lets us post-process the history (e.g. compact large
    file_write inputs) without relying on SDK internals.  Thinking-block
    signatures are preserved exactly — the API requires them to be echoed back
    unchanged on subsequent calls.
    """
    result: list[dict] = []
    for block in content:
        if isinstance(block, dict):
            result.append(block)
            continue
        block_type: str = getattr(block, "type", "")
        if not block_type:
            continue
        d: dict = {"type": block_type}
        if block_type == "text":
            d["text"] = getattr(block, "text", "")
        elif block_type == "thinking":
            d["thinking"] = getattr(block, "thinking", "")
            sig = getattr(block, "signature", None)
            if sig is not None:
                d["signature"] = sig
        elif block_type == "tool_use":
            d["id"] = getattr(block, "id", "")
            d["name"] = getattr(block, "name", "")
            d["input"] = dict(getattr(block, "input", None) or {})
        else:
            # Beta or future block variants — copy any known attributes
            for attr in ("id", "name", "input", "text", "thinking", "signature"):
                val = getattr(block, attr, None)
                if val is not None:
                    d[attr] = val
        result.append(d)
    return result


def _compact_file_writes(messages: list[dict], successful_ids: set[str]) -> None:
    """In-place: truncate large file_write tool_use content to a preview stub.

    Large file contents accumulate in the cache prefix and are re-billed at
    cache-creation rate on every subsequent turn.  Truncating to a 1 500-char
    preview keeps the cache footprint small while giving the model enough
    context to know what it already wrote (preventing pointless rewrites).
    If the content fits in 1 500 chars it is kept verbatim; otherwise it is
    followed by ``<TRUNCATED writtenChars=N>`` so the model knows the file is
    longer than the preview shown.

    Only the most recent assistant message is scanned — earlier messages are
    already part of the cached prefix and modifying them would cause a miss.
    """
    if not successful_ids:
        return
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            return
        new_content: list = []
        changed = False
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == "file_write"
                and block.get("id") in successful_ids
            ):
                inp = block.get("input") or {}
                path = inp.get("path", "?")
                raw = str(inp.get("content", ""))
                chars = len(raw)
                if chars > 1500:
                    stub_content = raw[:1500] + f"<TRUNCATED writtenChars={chars}>"
                else:
                    stub_content = raw
                new_content.append({
                    "type": "tool_use",
                    "id": block["id"],
                    "name": "file_write",
                    "input": {"path": path, "content": stub_content},
                })
                changed = True
            else:
                new_content.append(block)
        if changed:
            messages[i] = {**msg, "content": new_content}
        return  # only process the most recent assistant message


def _successful_write_ids(tool_use_blocks: list, tool_results: list[dict]) -> set[str]:
    """Return tool_use IDs for file_write calls that succeeded this turn."""
    write_ids = {
        getattr(b, "id", b.get("id") if isinstance(b, dict) else None)
        for b in tool_use_blocks
        if getattr(b, "name", b.get("name") if isinstance(b, dict) else "") == "file_write"
    }
    write_ids.discard(None)
    return {
        r["tool_use_id"]
        for r in tool_results
        if isinstance(r, dict)
        and r.get("type") == "tool_result"
        and r.get("tool_use_id") in write_ids
        and isinstance(r.get("content"), str)
        and not r["content"].lower().startswith("error")
    }


def _parse_final_output(text: str) -> tuple[dict[str, Any], str]:
    """Split model output into (structured JSON dict, prose text).

    System prompts instruct agents to return raw JSON, but the model often
    prepends a prose summary and wraps the JSON in a markdown fence.  This
    function extracts the JSON into ``structured`` and returns only the
    non-JSON prose as ``text`` so downstream consumers (reporter,
    prior_results) receive a readable summary rather than a raw blob.

    Extraction order:
      1. Direct JSON parse  — text IS the JSON; keep text as-is for display.
      2. Fenced code block  — strip the fence, return prose before it.
      3. Outermost { … }   — last resort; return prose before the brace.

    If no JSON is found both fields are returned unchanged.
    """
    if not text:
        return {}, text

    # 1. Direct parse — the whole text is JSON (model followed instructions).
    #    Preserve text so the reporter can display the formatted JSON string.
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed, text
    except (json.JSONDecodeError, TypeError):
        pass

    # 2. Fenced code block: ```[json]\n { ... } \n```
    tick_open = text.find("```")
    if tick_open != -1:
        newline_after_tick = text.find("\n", tick_open)
        tick_close = text.find("```", tick_open + 3)
        if newline_after_tick != -1 and tick_close > newline_after_tick:
            fenced = text[newline_after_tick + 1 : tick_close].strip()
            try:
                parsed = json.loads(fenced)
                if isinstance(parsed, dict):
                    prose = text[:tick_open].rstrip(" \n\r-")
                    return parsed, prose
            except (json.JSONDecodeError, TypeError):
                pass

    # 3. Outermost { … } span — handles mixed prose + inline JSON with no fence.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                prose = text[:start].rstrip(" \n\r-")
                return parsed, prose
        except (json.JSONDecodeError, TypeError):
            pass

    return {}, text


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
            turn_index=0,
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
        total_thinking_tokens = 0
        total_cache_creation_tokens = 0
        total_cache_read_tokens = 0
        total_cost_usd = 0.0
        final_text = ""
        last_response_content: list = []
        hit_limit = False

        task_budget = envelope.constraints.budget_usd
        max_iterations = self.manifest.max_iterations or settings.agent_max_iterations
        last_input_tokens = 0
        iteration = 0
        tool_call_counts: dict[str, int] = {}
        tool_limits: dict[str, int] = self.manifest.tool_limits or {}
        thinking_budget = self.manifest.thinking_budget_tokens

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

            if thinking_budget:
                create_kwargs["max_tokens"] = max(create_kwargs["max_tokens"], thinking_budget + 1024)
                create_kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
                if anthropic_tools:
                    create_kwargs["betas"] = ["interleaved-thinking-2025-05-14"]

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
            # The API doesn't expose a thinking token breakdown in usage; estimate from blocks.
            call_thinking = getattr(u, "thinking_tokens", 0) or sum(
                len(getattr(block, "thinking", "")) // 4
                for block in response.content
                if block.type == "thinking"
            )
            call_regular_output = u.output_tokens - call_thinking
            last_input_tokens = u.input_tokens
            total_input_tokens += u.input_tokens
            total_output_tokens += u.output_tokens
            total_thinking_tokens += call_thinking
            total_cache_creation_tokens += call_cache_write
            total_cache_read_tokens += call_cache_read
            total_cost_usd += (
                u.input_tokens * settings.cost_per_1m_input_tokens
                + call_regular_output * settings.cost_per_1m_output_tokens
                + call_thinking * settings.cost_per_1m_thinking_tokens
                + call_cache_write * settings.cost_per_1m_cache_write_tokens
                + call_cache_read * settings.cost_per_1m_cache_read_tokens
            ) / 1_000_000
            iteration += 1

            # Store the assistant response as plain dicts so the history can be
            # post-processed (e.g. file_write compaction) without SDK coupling.
            messages.append({"role": "assistant", "content": _to_dict_content(response.content)})
            last_response_content = list(response.content)

            # Emit thinking blocks as thought events so clients can display live reasoning.
            # Use block.type rather than isinstance so BetaThinkingBlock (beta endpoint)
            # and ThinkingBlock (standard endpoint) are both matched.
            if thinking_budget:
                for block in response.content:
                    thinking_text = getattr(block, "thinking", None)
                    if block.type == "thinking" and thinking_text and thinking_text.strip():
                        emitter.emit(SSEEventType.agent_thought, agent_id=self.agent_id, message=thinking_text, turn_index=iteration)

            # Collect any text the model produced this turn (BetaTextBlock or TextBlock)
            for block in response.content:
                if block.type == "text":
                    final_text = getattr(block, "text", "")

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason != "tool_use":
                logger.warning(
                    "[%s] Unexpected stop_reason %r at iteration %d",
                    self.agent_id, response.stop_reason, iteration,
                )
                if response.stop_reason == "max_tokens":
                    hit_limit = True
                # Execute any tool_use blocks that are present before stopping so the
                # message history stays valid for continuation (every tool_use must be
                # immediately followed by a tool_result in the next message).
                pending_tool_use = [b for b in response.content if b.type == "tool_use"]
                if pending_tool_use:
                    pending_results = await asyncio.gather(
                        *[self._checked_call_tool(b, tools, emitter, tool_call_counts, tool_limits, iteration)
                          for b in pending_tool_use]
                    )
                    messages.append({"role": "user", "content": list(pending_results)})
                    _compact_file_writes(
                        messages,
                        _successful_write_ids(pending_tool_use, list(pending_results)),
                    )
                break

            # Emit any text the model produced alongside tool calls as a thought event
            for block in response.content:
                text = getattr(block, "text", None)
                if block.type == "text" and text and text.strip():
                    emitter.emit(
                        SSEEventType.agent_thought,
                        agent_id=self.agent_id,
                        message=text,
                        turn_index=iteration,
                    )

            # Execute all tool calls concurrently, then feed results back
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            tool_results = await asyncio.gather(
                *[self._checked_call_tool(b, tools, emitter, tool_call_counts, tool_limits, iteration)
                  for b in tool_use_blocks]
            )

            messages.append({"role": "user", "content": list(tool_results)})
            # Compact successful file_write inputs so large file contents don't
            # accumulate in the cache prefix across subsequent turns.
            _compact_file_writes(
                messages,
                _successful_write_ids(tool_use_blocks, list(tool_results)),
            )

            # Inject any pending user message as an additional user turn before
            # the next API call, so the model sees it without a separate round-trip.
            if ctx is not None:
                pending = await ctx.pop_user_message(self.agent_id)
                if pending is not None:
                    messages.append({"role": "user", "content": pending})
                    emitter.emit(SSEEventType.run_message_received, agent_id=self.agent_id, message=pending[:120], turn_index=iteration)

        # With extended thinking the model may end via thinking + tool use without ever
        # producing a text block.  Fall back to the last thinking block so the reporter
        # receives a meaningful summary instead of an empty string.
        if not final_text and thinking_budget:
            for block in reversed(last_response_content):
                thinking_text = getattr(block, "thinking", None)
                if block.type == "thinking" and thinking_text and thinking_text.strip():
                    final_text = thinking_text
                    break

        # Extract structured JSON and clean prose from the final model output.
        # Agents are instructed to return raw JSON, but often prepend a summary
        # and wrap the JSON in a markdown fence; _parse_final_output handles all
        # three cases and strips the JSON block from the prose text.
        structured, final_text = _parse_final_output(final_text)

        return AgentResult(
            task_id=envelope.task_id,
            agent_id=self.agent_id,
            status=AgentStatus.partial if hit_limit else AgentStatus.success,
            output=AgentOutput(structured=structured, text=final_text),
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            thinking_tokens=total_thinking_tokens,
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
        turn_index: int,
    ) -> dict[str, Any]:
        emitter.emit(
            SSEEventType.agent_progress,
            agent_id=self.agent_id,
            message=f"Calling tool: {block.name}",
            data={"tool": block.name, "input": block.input},
            turn_index=turn_index,
            tool_call_id=block.id,
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
        emitter.emit(
            SSEEventType.agent_tool_result,
            agent_id=self.agent_id,
            message=result_text[:200],
            data={"tool": block.name, "result": result_text},
            turn_index=turn_index,
            tool_call_id=block.id,
        )
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": result_text,
        }

    async def _checked_call_tool(
        self,
        block: Any,
        tools: list,
        emitter: "StreamEmitter",
        counts: dict[str, int],
        limits: dict[str, int],
        turn_index: int,
    ) -> dict[str, Any]:
        """Call a tool, enforcing manifest tool_limits before dispatch."""
        if block.name in limits:
            counts[block.name] = counts.get(block.name, 0) + 1
            if counts[block.name] > limits[block.name]:
                logger.warning(
                    "[%s] Tool budget exhausted: '%s' limited to %d call(s), this would be call %d",
                    self.agent_id, block.name, limits[block.name], counts[block.name],
                )
                budget_error = (
                    f"Tool budget exhausted: '{block.name}' is limited to "
                    f"{limits[block.name]} call(s) per task. "
                    "Use information already gathered instead of making additional calls."
                )
                emitter.emit(
                    SSEEventType.agent_tool_result,
                    agent_id=self.agent_id,
                    message=budget_error,
                    data={"tool": block.name, "result": budget_error, "budget_exhausted": True},
                    turn_index=turn_index,
                    tool_call_id=block.id,
                )
                return {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": budget_error,
                }
        return await self._call_tool(block, tools, emitter, turn_index)
