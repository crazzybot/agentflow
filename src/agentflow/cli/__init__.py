"""AgentFlow CLI — submit tasks and stream results."""
from __future__ import annotations

import asyncio
import sys
from typing import Any

import click

from .client import AgentFlowError, check_health, start_run, stream_events
from .display import RunDisplay, console


@click.group()
@click.option("--host", default="127.0.0.1", envvar="AGENTFLOW_HOST", show_default=True, help="Service host")
@click.option("--port", default=8001, envvar="AGENTFLOW_PORT", show_default=True, type=int, help="Service port")
@click.pass_context
def main(ctx: click.Context, host: str, port: int) -> None:
    """AgentFlow — multi-agent orchestration CLI."""
    ctx.ensure_object(dict)
    ctx.obj["host"] = host
    ctx.obj["port"] = port
    ctx.obj["base_url"] = f"http://{host}:{port}"


@main.command()
@click.argument("task")
@click.option("--context", "-c", multiple=True, metavar="KEY=VALUE", help="Context key=value pairs (repeatable)")
@click.option("--verbose", "-v", is_flag=True, help="Show agent progress events")
@click.option("--json", "output_json", is_flag=True, help="Output raw JSON events")
@click.pass_context
def run(ctx: click.Context, task: str, context: tuple[str, ...], verbose: bool, output_json: bool) -> None:
    """Submit TASK to the orchestrator and stream results."""
    base_url = ctx.obj["base_url"]

    ctx_dict: dict[str, Any] = {}
    for item in context:
        if "=" not in item:
            raise click.BadParameter(f"must be KEY=VALUE, got: {item!r}", param_hint="--context")
        k, _, v = item.partition("=")
        ctx_dict[k] = v

    asyncio.run(_run_task(base_url, task, ctx_dict, verbose, output_json))


async def _run_task(
    base_url: str,
    task: str,
    context: dict[str, Any],
    verbose: bool,
    output_json: bool,
) -> None:
    display = RunDisplay(output_json=output_json, verbose=verbose)

    try:
        result = await start_run(base_url, task, context)
    except AgentFlowError as e:
        display.error(str(e))
        sys.exit(1)

    run_id = result["run_id"]
    display.run_started(run_id, task)

    try:
        async for event in stream_events(base_url, run_id):
            display.handle_event(event)
            if event.get("type") in ("run:complete", "run:error", "run:cancelled"):
                break
    except KeyboardInterrupt:
        display.interrupted()
    except AgentFlowError as e:
        display.error(str(e))
        sys.exit(1)


@main.command()
@click.pass_context
def health(ctx: click.Context) -> None:
    """Check service health and list registered agents."""
    base_url = ctx.obj["base_url"]
    asyncio.run(_health(base_url))


async def _health(base_url: str) -> None:
    try:
        data = await check_health(base_url)
    except AgentFlowError as e:
        console.print(f"[bold red]✗[/bold red] {e}")
        sys.exit(1)

    console.print(f"[bold green]●[/bold green] AgentFlow is healthy at [cyan]{base_url}[/cyan]")
    agents = data.get("agents", [])
    if agents:
        console.print(f"\n[bold]Agents[/bold] ({len(agents)} loaded):")
        for agent_id in agents:
            console.print(f"  [dim]·[/dim] {agent_id}")
    else:
        console.print("  [dim]No agents loaded[/dim]")


@main.command()
@click.option("--reload", is_flag=True, help="Enable auto-reload on file changes")
@click.pass_context
def serve(ctx: click.Context, reload: bool) -> None:
    """Start the AgentFlow server."""
    import uvicorn

    host = ctx.obj["host"]
    port = ctx.obj["port"]
    uvicorn.run("agentflow.main:app", host=host, port=port, reload=reload, log_level="info")
