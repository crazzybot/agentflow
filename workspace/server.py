"""
hello-world-mcp — A minimal MCP server demonstrating tools, resources, and prompts.

Run in development mode:
    uv run mcp dev server.py

Run directly:
    uv run python server.py
"""

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Server initialisation
# ---------------------------------------------------------------------------
mcp = FastMCP("hello-world-mcp")


# ---------------------------------------------------------------------------
# Tool — callable functions exposed to LLM clients
# ---------------------------------------------------------------------------
@mcp.tool()
def say_hello(name: str) -> str:
    """Return a friendly greeting for the given name.

    Args:
        name: The name of the person to greet.

    Returns:
        A greeting string.
    """
    return f"Hello, {name}! Welcome to your first MCP server."


# ---------------------------------------------------------------------------
# Resource — read-only data endpoints exposed to LLM clients
# ---------------------------------------------------------------------------
@mcp.resource("info://server")
def server_info() -> str:
    """Return a brief description of what this server does."""
    return (
        "This is a hello-world MCP server. "
        "It demonstrates the three core MCP primitives: tools, resources, and prompts."
    )


# ---------------------------------------------------------------------------
# Prompt — reusable message templates for LLM interactions
# ---------------------------------------------------------------------------
@mcp.prompt()
def greeting_prompt(name: str) -> str:
    """Generate a prompt that asks an LLM to greet a user warmly.

    Args:
        name: The name of the person to greet.

    Returns:
        A prompt string ready to be sent to an LLM.
    """
    return f"Please write a warm, friendly greeting for someone named {name}."


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Runs over stdio by default — compatible with Claude Desktop and the
    # MCP Inspector when launched via `mcp dev server.py`.
    mcp.run()
