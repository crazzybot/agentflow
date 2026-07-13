"""Rich terminal display for AgentFlow runs."""
from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

_ICONS: dict[str, str] = {
    "run:started": "◆",
    "plan:created": "◇",
    "task:dispatched": "→",
    "agent:progress": "·",
    "agent:query": "?",
    "task:complete": "✓",
    "task:failed": "✗",
    "run:complete": "◆",
    "run:error": "✗",
}

_STYLES: dict[str, str] = {
    "run:started": "bold cyan",
    "plan:created": "cyan",
    "task:dispatched": "blue",
    "agent:progress": "dim",
    "agent:query": "yellow",
    "task:complete": "green",
    "task:failed": "bold red",
    "run:complete": "bold green",
    "run:error": "bold red",
}


class RunDisplay:
    def __init__(self, output_json: bool = False, verbose: bool = False) -> None:
        self.output_json = output_json
        self.verbose = verbose
        self._plan: dict[str, dict[str, Any]] = {}  # subtask_id → subtask dict

    def run_started(self, run_id: str, task: str) -> None:
        if self.output_json:
            return
        console.print()
        console.print(f"[bold]Task:[/bold] {task}")
        console.print(f"[dim]Run ID: {run_id}[/dim]")
        console.print()

    def handle_event(self, event: dict[str, Any]) -> None:
        if self.output_json:
            console.print_json(json.dumps(event))
            return

        event_type: str = event.get("type", "")
        payload: dict[str, Any] = event.get("payload", {})
        agent_id: str | None = event.get("agent_id")
        message: str = payload.get("message", "")
        data: Any = payload.get("data")

        if event_type == "plan:created":
            self._handle_plan_created(data)

        elif event_type == "task:dispatched":
            subtask_id = (data or {}).get("subtask_id", "")
            self._print(event_type, agent_id, f"dispatching {subtask_id}")

        elif event_type in ("task:complete", "task:failed"):
            subtask_id = (data or {}).get("subtask_id", "")
            label = f"{subtask_id} complete" if event_type == "task:complete" else f"{subtask_id} failed"
            self._print(event_type, agent_id, label)

        elif event_type == "agent:progress":
            if self.verbose and message:
                self._print(event_type, agent_id, message)

        elif event_type == "agent:tool_result":
            if self.verbose and data and isinstance(data, dict):
                tool = data.get("tool", "")
                result = data.get("result", "")
                preview = result[:120].replace("\n", " ")
                self._print(event_type, agent_id, f"← {tool}: {preview}")

        elif event_type == "agent:query":
            self._print(event_type, agent_id, message)

        elif event_type == "run:complete":
            self._handle_run_complete(message, data)

        elif event_type == "run:error":
            console.print()
            self._print(event_type, None, message)
            if data and isinstance(data, dict):
                for subtask_id, err in data.items():
                    console.print(f"  [dim]{subtask_id}:[/dim] [red]{err}[/red]")

    def _handle_plan_created(self, data: Any) -> None:
        if not data:
            return
        subtasks: list[dict[str, Any]] = data.get("subtasks", [])
        for st in subtasks:
            self._plan[st["id"]] = st

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("ID", style="dim", no_wrap=True)
        table.add_column("Agent", style="cyan", no_wrap=True)
        table.add_column("Instruction")
        table.add_column("Depends on", style="dim")

        for st in subtasks:
            deps = ", ".join(st.get("depends_on", [])) or "—"
            instruction = st.get("instruction", "")
            if len(instruction) > 60:
                instruction = instruction[:57] + "..."
            table.add_row(st["id"], st["agent_id"], instruction, deps)

        console.print(f"[cyan]◇[/cyan]  Plan — {len(subtasks)} subtask(s)\n")
        console.print(table)
        console.print()

    def _handle_run_complete(self, message: str, data: Any) -> None:
        console.print()
        console.print(f"[bold green]◆[/bold green]  {message}")

        if not data or not isinstance(data, dict):
            return

        # Partial success: {"results": {...}, "failed": {...}}
        if "results" in data:
            results: dict[str, Any] = data["results"]
            failed: dict[str, Any] = data.get("failed", {})
            if failed:
                console.print(f"  [yellow]Failed:[/yellow] {', '.join(failed)}")
        else:
            results = data

        for subtask_id, output in results.items():
            text = output.get("text", "") if isinstance(output, dict) else ""
            if text and text.strip():
                st_info = self._plan.get(subtask_id, {})
                title = f"[cyan]{st_info.get('agent_id', subtask_id)}[/cyan]  {subtask_id}"
                console.print()
                console.print(Panel(text.strip(), title=title, border_style="dim", padding=(0, 1)))

    def _print(self, event_type: str, agent_id: str | None, message: str) -> None:
        icon = _ICONS.get(event_type, "·")
        style = _STYLES.get(event_type, "")
        agent_part = f"[dim]{agent_id}[/dim]  " if agent_id else ""
        console.print(f"  [{style}]{icon}[/{style}]  {agent_part}{message}")

    def error(self, message: str) -> None:
        console.print(f"\n[bold red]Error:[/bold red] {message}")

    def interrupted(self) -> None:
        console.print("\n[yellow]Interrupted[/yellow]")
