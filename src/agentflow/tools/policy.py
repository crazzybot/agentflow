"""Pre-tool-use policies: a per-invocation chain checked before every tool call.

``build_policies(manifest)`` turns a manifest's declarations into an ordered list
of ``ToolPolicy`` objects; ``Agent._checked_call_tool`` runs them before
dispatching a call, and the first one to object blocks it — the model receives
the policy's reason as the tool result and the tool never runs.

Policies:

- ``CallLimitPolicy`` — ``tool_limits`` per-task call budgets (stateful).
- ``CommandAllowlistPolicy`` — ``tool_policies[].allow_commands`` for shell tools:
  every command in a pipeline/list must be on the list. Parsing is fail-closed:
  command substitution, process substitution, subshells, newlines and
  unparseable quoting are refused, because they could hide a command the
  allowlist never sees.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
from collections.abc import Collection
from typing import Any, Protocol

from agentflow.core.models import AgentManifest

logger = logging.getLogger(__name__)

_SEPARATORS = frozenset({"|", "||", "&&", ";", "&", "|&"})
_REDIRECTS = frozenset({">", ">>", "<", "<<", "<<<", ">&", "<&", "&>", "&>>", ">|"})
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# Arguments that make an allowed command run another, unlisted program.
_EXEC_ARGS: dict[str, frozenset[str]] = {
    "find": frozenset({"-exec", "-execdir", "-ok", "-okdir"}),
}
# Allowlisting any of these effectively allows everything: warned about at build time.
_COMMAND_RUNNERS = frozenset({
    "sh", "bash", "zsh", "dash", "env", "xargs", "exec", "eval", "sudo", "nohup",
    "timeout", "nice", "python", "python3", "node", "perl", "ruby", "make", "npx",
})


class ToolPolicy(Protocol):
    name: str

    def check(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        """Reason to block this call, or None to allow it."""
        ...


class CallLimitPolicy:
    name = "tool_limits"

    def __init__(self, limits: dict[str, int]) -> None:
        self.limits = limits
        self.counts: dict[str, int] = {}

    def check(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        if tool_name not in self.limits:
            return None
        self.counts[tool_name] = self.counts.get(tool_name, 0) + 1
        if self.counts[tool_name] <= self.limits[tool_name]:
            return None
        return (
            f"Tool budget exhausted: '{tool_name}' is limited to {self.limits[tool_name]} call(s) "
            "per task. Use information already gathered instead of making additional calls."
        )


def _substitution_error(command: str) -> str | None:
    """Refuse constructs that run commands the stage parser cannot see."""
    if "\n" in command or "\r" in command:
        return "multi-line commands are not allowed here; join commands with ';' or '&&'"
    quote: str | None = None
    escaped = False
    prev = ""
    for ch in command:
        if escaped:
            escaped, prev = False, ""
            continue
        if quote == "'":
            if ch == "'":
                quote = None
            prev = ch
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "`" or (ch == "(" and prev in ("$", "<", ">")):
            return "command or process substitution is not allowed here"
        if quote is None and ch in ("'", '"'):
            quote = ch
        elif quote == '"' and ch == '"':
            quote = None
        prev = ch
    return None


def shell_commands(command: str) -> list[tuple[str, list[str]]]:
    """(command name, args) for every simple command in a shell pipeline/list.

    Raises ValueError for anything that cannot be attributed to named commands.
    """
    if err := _substitution_error(command):
        raise ValueError(err)
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise ValueError(f"could not parse command: {exc}") from exc

    stages: list[list[str]] = [[]]
    for tok in tokens:
        if tok in ("(", ")") or tok.startswith("("):
            raise ValueError("subshells are not allowed here")
        if tok in _SEPARATORS:
            stages.append([])
        else:
            stages[-1].append(tok)

    commands: list[tuple[str, list[str]]] = []
    for stage in stages:
        i = 0
        while i < len(stage):  # skip VAR=value prefixes and leading redirections
            if _ASSIGNMENT_RE.match(stage[i]):
                i += 1
            elif stage[i] in _REDIRECTS:
                i += 2
            else:
                break
        if i < len(stage):
            commands.append((os.path.basename(stage[i]), stage[i + 1:]))
    return commands


class CommandAllowlistPolicy:
    name = "allow_commands"

    def __init__(self, tool: str, allowed: Collection[str]) -> None:
        self.tool = tool
        self.allowed = frozenset(allowed)
        risky = sorted(self.allowed & _COMMAND_RUNNERS)
        if risky:
            logger.warning(
                "Command allowlist for %s includes %s, which can run arbitrary commands",
                tool, ", ".join(risky),
            )

    def check(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        if tool_name != self.tool:
            return None
        command = str(tool_input.get("command", ""))
        try:
            commands = shell_commands(command)
        except ValueError as exc:
            return f"Blocked by policy: {exc}."
        allowed = ", ".join(sorted(self.allowed))
        for name, args in commands:
            if name not in self.allowed:
                return f"Blocked by policy: '{name}' is not an allowed command for {self.tool}. Allowed: {allowed}."
            forbidden = _EXEC_ARGS.get(name, frozenset()) & set(args)
            if forbidden:
                return f"Blocked by policy: '{name} {sorted(forbidden)[0]}' would run an unlisted command."
        return None


def build_policies(manifest: AgentManifest) -> list[ToolPolicy]:
    """The ordered policy chain for one agent invocation (fresh state each time)."""
    policies: list[ToolPolicy] = []
    for config in manifest.tool_policies:
        if config.allow_commands is not None:
            policies.append(CommandAllowlistPolicy(config.tool, config.allow_commands))
    if manifest.tool_limits:
        # Last: a call blocked by an earlier policy does not use up the budget.
        policies.append(CallLimitPolicy(manifest.tool_limits))
    return policies
