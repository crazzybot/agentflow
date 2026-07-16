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
from agentflow.core.models import AgentManifest, AgentOutput, AgentResult, AgentStatus, IterationLimitAction, SSEEventType, TaskEnvelope
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

# Injected as a user turn when on_iteration_limit == "finalize" and the agent
# exhausts its iteration budget without producing a final text response.
_DEFAULT_FINALIZE_MESSAGE = (
    "You have reached your iteration limit and cannot make further tool calls. "
    "Based on all the information you have gathered so far, produce your final output now. "
    "Do not request any more tools — write your response directly."
)


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


def _collect_written_paths(tool_use_blocks: list, tool_results: list[dict]) -> list[str]:
    """Return file paths successfully written by file_write calls this turn."""
    write_blocks: dict[str, str] = {}
    for b in tool_use_blocks:
        b_id = getattr(b, "id", b.get("id") if isinstance(b, dict) else None)
        b_name = getattr(b, "name", b.get("name") if isinstance(b, dict) else "")
        if b_name == "file_write" and b_id:
            inp = getattr(b, "input", b.get("input") if isinstance(b, dict) else {}) or {}
            path = inp.get("path", "")
            if path:
                write_blocks[b_id] = path

    return [
        write_blocks[r["tool_use_id"]]
        for r in tool_results
        if isinstance(r, dict)
        and r.get("type") == "tool_result"
        and r.get("tool_use_id") in write_blocks
        and isinstance(r.get("content"), str)
        and not r["content"].lower().startswith("error")
    ]


def _format_upstream_context(
    prior_results: dict[str, Any],
    upstream_artifacts: dict[str, list[str]],
) -> str:
    """Build the <upstream_context> block for the initial user message.

    Combines text summaries from prior_results with file paths from
    upstream_artifacts so downstream agents know both what happened and
    where the output files are.
    """
    if not prior_results and not upstream_artifacts:
        return ""
    all_dep_ids = sorted(set(prior_results) | set(upstream_artifacts))
    lines: list[str] = []
    for dep_id in all_dep_ids:
        lines.append(f'Upstream task "{dep_id}":')
        summary = prior_results.get(dep_id, "")
        if summary:
            lines.append(f"  Summary: {str(summary)[:500]}")
        paths = upstream_artifacts.get(dep_id, [])
        if paths:
            lines.append("  Files written:")
            for p in paths:
                lines.append(f"    - {p}")
    return "\n\n<upstream_context>\n" + "\n".join(lines) + "\n</upstream_context>"


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

    # 3. Outermost { … } span — iterate left-to-right through { positions so that
    #    prose containing bare { (e.g. shell brace expansions like {a,b,c}, path
    #    globs, or template literals) doesn't prevent finding the JSON object.
    #    The end anchor is always the last } in the text (rfind), and we advance
    #    the start candidate until json.loads succeeds.
    end = text.rfind("}")
    if end != -1:
        pos = 0
        while True:
            start = text.find("{", pos)
            if start == -1 or start >= end:
                break
            try:
                parsed = json.loads(text[start : end + 1])
                if isinstance(parsed, dict):
                    prose = text[:start].rstrip(" \n\r-")
                    return parsed, prose
            except (json.JSONDecodeError, TypeError):
                pos = start + 1

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
    return max(256, min(settings.agent_max_tokens_cap, tokens))



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
            # Resume a partial run — continue from the existing message thread.
            messages: list[dict[str, Any]] = list(resume_messages)
            # If the last message is from the assistant the model paused mid-thought;
            # add a user prompt to continue.  If it is a user message (tool results)
            # the model will naturally process them on the next iteration.
            if messages and messages[-1].get("role") == "assistant":
                messages.append({
                    "role": "user",
                    "content": "You reached the iteration limit. Continue your work from where you left off — do not repeat completed steps.",
                })
        else:
            # Build the initial user message with upstream context and optional user context.
            user_content = envelope.instruction
            if envelope.context.user_context:
                user_content += (
                    "\n\n<user_context>\n"
                    + json.dumps(envelope.context.user_context, separators=(",", ":"))
                    + "\n</user_context>"
                )
            upstream_block = _format_upstream_context(
                envelope.context.prior_results,
                envelope.context.upstream_artifacts,
            )
            if upstream_block:
                user_content += upstream_block
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
        hit_max_tokens = False
        _finalizing = False  # True during the single extra LLM call for on_iteration_limit="finalize"
        all_files_written: list[str] = []

        task_budget = envelope.constraints.budget_usd
        max_iterations = self.manifest.max_iterations or settings.agent_max_iterations
        last_input_tokens = 0
        iteration = 0
        tool_call_counts: dict[str, int] = {}
        tool_limits: dict[str, int] = self.manifest.tool_limits or {}
        thinking_budget = self.manifest.thinking_budget_tokens or (settings.agent_thinking_budget_tokens or None)

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
                if not _finalizing and iteration >= max_iterations:
                    action = self.manifest.on_iteration_limit
                    if action == IterationLimitAction.finalize:
                        finalize_msg = self.manifest.iteration_limit_message or _DEFAULT_FINALIZE_MESSAGE
                        messages.append({"role": "user", "content": finalize_msg})
                        _finalizing = True
                        hit_limit = True
                        logger.warning(
                            "[%s] Hit max iterations (%d) — injecting finalization prompt",
                            self.agent_id, max_iterations,
                        )
                    elif action == IterationLimitAction.ask_user and ctx is not None:
                        async with ctx.human_input_lock:
                            ctx.request_human_input()
                            emitter.emit(
                                SSEEventType.run_awaiting_input,
                                agent_id=self.agent_id,
                                message=(
                                    f"Agent '{self.agent_id}' hit its iteration limit ({max_iterations}). "
                                    "Reply with action='continue' and iteration_increase=<N> for more "
                                    "iterations, or action='cancel'."
                                ),
                            )
                            hitl_response = await ctx.await_human_input()
                        if hitl_response.action == "continue":
                            extra = hitl_response.iteration_increase or 5
                            max_iterations += extra
                            logger.info(
                                "[%s] User granted %d more iterations (new limit: %d)",
                                self.agent_id, extra, max_iterations,
                            )
                        else:
                            hit_limit = True
                            logger.warning(
                                "[%s] User cancelled at iteration limit (%d) — returning partial result",
                                self.agent_id, max_iterations,
                            )
                            break
                    else:
                        hit_limit = True
                        logger.warning(
                            "[%s] Hit max iterations (%d) — returning partial result",
                            self.agent_id, max_iterations,
                        )
                        break
                max_tokens = settings.agent_max_tokens_fallback

            create_kwargs: dict[str, Any] = {
                "model": self.manifest.model or settings.agent_model,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": _with_message_cache_breakpoint(messages),
            }
            if anthropic_tools:
                create_kwargs["tools"] = anthropic_tools
                if _finalizing:
                    # Disable tool use so the model is forced to produce text output.
                    create_kwargs["tool_choice"] = {"type": "none"}

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

            # Collect any text the model produced this turn (BetaTextBlock or TextBlock)
            final_text = ""
            for block in response.content:
                if block.type == "text":
                    final_text += getattr(block, "text", "")

            if response.stop_reason == "end_turn":
                break

            if _finalizing:
                # The finalization LLM call is complete regardless of stop_reason.
                logger.debug("[%s] Finalization turn complete (stop_reason=%r)", self.agent_id, response.stop_reason)
                break

            if response.stop_reason != "tool_use":
                logger.warning(
                    "[%s] Unexpected stop_reason %r at iteration %d",
                    self.agent_id, response.stop_reason, iteration,
                )
                if response.stop_reason == "max_tokens":
                    hit_limit = True
                    hit_max_tokens = True
                    # The model was cut off mid-generation — any tool_use blocks in the
                    # response may have truncated inputs (e.g. file_write missing content).
                    # Remove the partial assistant message from history so that
                    # continuation resumes from the last clean state rather than
                    # replaying a broken call that will fail with a missing-argument error.
                    if messages and messages[-1].get("role") == "assistant":
                        messages.pop()
                    # Still execute the tool calls so SSE events reach the client,
                    # but discard the results — they must not enter the history.
                    pending_tool_use = [b for b in response.content if b.type == "tool_use"]
                    if pending_tool_use:
                        await asyncio.gather(
                            *[self._checked_call_tool(b, tools, emitter, tool_call_counts, tool_limits, iteration)
                              for b in pending_tool_use]
                        )
                else:
                    # For other unexpected stop reasons, execute any pending tool calls so
                    # the message history stays valid (every tool_use must be immediately
                    # followed by a tool_result in the next message).
                    pending_tool_use = [b for b in response.content if b.type == "tool_use"]
                    if pending_tool_use:
                        pending_results = await asyncio.gather(
                            *[self._checked_call_tool(b, tools, emitter, tool_call_counts, tool_limits, iteration)
                              for b in pending_tool_use]
                        )
                        messages.append({"role": "user", "content": list(pending_results)})
                        all_files_written.extend(
                            _collect_written_paths(pending_tool_use, list(pending_results))
                        )
                break

            # Emit thinking blocks as thought events so clients can display live reasoning.
            # Use block.type rather than isinstance so BetaThinkingBlock (beta endpoint)
            # and ThinkingBlock (standard endpoint) are both matched.
            thinking_text = ""
            if thinking_budget:
                for block in response.content:
                    text = None
                    if block.type == "thinking":
                        text = getattr(block, "thinking", None)
                    elif block.type == "text":
                        text = getattr(block, "text", None)
                    
                    if text:
                        thinking_text += text
            if thinking_text:
                emitter.emit(SSEEventType.agent_thought, agent_id=self.agent_id, message=thinking_text, turn_index=iteration)

            # Execute all tool calls concurrently, then feed results back
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            tool_results = await asyncio.gather(
                *[self._checked_call_tool(b, tools, emitter, tool_call_counts, tool_limits, iteration)
                  for b in tool_use_blocks]
            )

            messages.append({"role": "user", "content": list(tool_results)})
            all_files_written.extend(
                _collect_written_paths(tool_use_blocks, list(tool_results))
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
            final_text = ""
            for block in reversed(last_response_content):
                if block.type == "thinking":
                    text = getattr(block, "thinking", "")
                    if thinking_text and text.strip():
                        final_text += thinking_text

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
            hit_max_tokens=hit_max_tokens,
            files_written=all_files_written,
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
