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
from typing import TYPE_CHECKING, Any

import anthropic

from agentflow.config import settings
from agentflow.core.models import AgentManifest, AgentOutput, AgentResult, AgentStatus, TaskEnvelope
from agentflow.llm import LLMClient
from agentflow.tools import tool_registry
from agentflow.tools.mcp_tools import mcp_session

if TYPE_CHECKING:
    from agentflow.orchestrator.stream import StreamEmitter

logger = logging.getLogger(__name__)


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

    async def run(self, envelope: TaskEnvelope, emitter: "StreamEmitter") -> AgentResult:
        from agentflow.core.models import SSEEventType

        start_ms = int(time.time() * 1000)
        emitter.emit(
            SSEEventType.agent_progress,
            agent_id=self.agent_id,
            message=f"Starting: {envelope.instruction[:80]}",
        )
        try:
            result = await self._execute(envelope, emitter)
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

    async def _execute(self, envelope: TaskEnvelope, emitter: "StreamEmitter") -> AgentResult:
        async with AsyncExitStack() as stack:
            # 1. Gather tool definitions from the global registry (filtered by manifest)
            local_tools = tool_registry.get_many(self.manifest.tools)

            # 2. Connect to each MCP server and collect their tools
            mcp_tools: list = []
            for server_config in self.manifest.mcp_servers:
                server_tool_defs = await stack.enter_async_context(mcp_session(server_config))
                mcp_tools.extend(server_tool_defs)

            all_tools = local_tools + mcp_tools

            # 3. Run the agentic loop
            return await self._agentic_loop(envelope, all_tools, emitter)

    # ------------------------------------------------------------------
    # Agentic loop with tool execution
    # ------------------------------------------------------------------

    async def _agentic_loop(
        self,
        envelope: TaskEnvelope,
        tools: list,
        emitter: "StreamEmitter",
    ) -> AgentResult:
        from agentflow.core.models import SSEEventType

        # Build the initial user message, injecting prior results as context
        user_content = envelope.instruction
        if envelope.context.prior_results:
            user_content += (
                "\n\n<context>\n"
                + json.dumps(envelope.context.prior_results, indent=2)
                + "\n</context>"
            )

        messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
        anthropic_tools = [t.to_anthropic_param() for t in tools]
        total_tokens = 0
        final_text = ""

        for iteration in range(settings.agent_max_iterations):
            create_kwargs: dict[str, Any] = {
                "model": settings.agent_model,
                "max_tokens": envelope.constraints.max_tokens,
                "system": self.manifest.system_prompt,
                "messages": messages,
            }
            if anthropic_tools:
                create_kwargs["tools"] = anthropic_tools

            response = await self.client.messages.create(**create_kwargs)
            total_tokens += response.usage.input_tokens + response.usage.output_tokens

            # Append the assistant's full response (preserves tool_use blocks)
            messages.append({"role": "assistant", "content": response.content})

            # Collect any text the model produced this turn
            for block in response.content:
                if hasattr(block, "text"):
                    final_text = block.text  # keep the last text seen

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason != "tool_use":
                logger.warning(
                    "[%s] Unexpected stop_reason %r at iteration %d",
                    self.agent_id, response.stop_reason, iteration,
                )
                break

            # Execute all tool calls concurrently, then feed results back
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            tool_results = await asyncio.gather(
                *[self._call_tool(b, tools, emitter) for b in tool_use_blocks]
            )

            messages.append({"role": "user", "content": list(tool_results)})

        else:
            logger.warning("[%s] Hit max iterations (%d)", self.agent_id, settings.agent_max_iterations)

        # Try to parse final text as JSON (many system prompts ask for JSON output)
        structured: dict[str, Any] = {}
        try:
            structured = json.loads(final_text)
        except (json.JSONDecodeError, TypeError):
            pass

        return AgentResult(
            task_id=envelope.task_id,
            agent_id=self.agent_id,
            status=AgentStatus.success,
            output=AgentOutput(structured=structured, text=final_text),
            tokens_used=total_tokens,
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
        from agentflow.core.models import SSEEventType

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

        logger.debug("[%s] Tool %r → %s…", self.agent_id, block.name, result_text[:80])
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": result_text,
        }
