"""
hello_world MCP Server
======================
Implements the Model Context Protocol (spec 2025-11-25) over stdio transport.

Lifecycle
---------
1. Client  →  initialize (request)
2. Server  →  initialize (response)   ← ServerCapabilities declared here
3. Client  →  initialized (notification)
4. Client  →  tools/list  (request)
5. Server  →  tools/list  (response)
6. Client  →  tools/call  (request)
7. Server  →  tools/call  (response)

Wire format: newline-delimited JSON-RPC 2.0 on stdin / stdout.
All log output goes to stderr so it never pollutes the JSON-RPC stream.
"""

import json
import sys
import logging

# ── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    stream=sys.stderr,
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("mcp-hello-world")

# ── protocol constants ────────────────────────────────────────────────────────
PROTOCOL_VERSION = "2025-11-25"
JSONRPC_VERSION = "2.0"

SERVER_INFO = {"name": "hello-world-mcp", "version": "1.0.0"}

SERVER_CAPABILITIES = {"tools": {}}  # we advertise the 'tools' capability

# ── tool registry ─────────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "hello_world",
        "description": (
            "Returns a friendly greeting message. "
            "Pass an optional 'name' to personalise the greeting."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The name of the person to greet (default: 'World').",
                }
            },
            "required": [],
            "additionalProperties": False,
        },
    }
]


# ── tool implementation ───────────────────────────────────────────────────────
def tool_hello_world(params: dict) -> dict:
    """
    Execute the hello_world tool.

    Returns a JSON-RPC-compatible tool result:
    { "content": [{"type": "text", "text": "..."}], "isError": false }
    """
    name = (params.get("arguments") or {}).get("name", "World")

    # Input validation – name must be a non-empty string if provided
    if not isinstance(name, str):
        return {
            "content": [{"type": "text", "text": "Error: 'name' must be a string."}],
            "isError": True,
        }
    name = name.strip() or "World"

    greeting = f"Hello, {name}! 👋  Welcome to the Model Context Protocol."
    log.info("hello_world called → %s", greeting)
    return {
        "content": [{"type": "text", "text": greeting}],
        "isError": False,
    }


# ── JSON-RPC helpers ──────────────────────────────────────────────────────────
def make_response(req_id, result: dict) -> dict:
    return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "result": result}


def make_error(req_id, code: int, message: str) -> dict:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": req_id,
        "error": {"code": code, "message": message},
    }


def send(obj: dict) -> None:
    """Serialise *obj* as a single JSON line on stdout."""
    line = json.dumps(obj, ensure_ascii=False)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    log.debug("SEND → %s", line)


# ── request dispatcher ────────────────────────────────────────────────────────
def handle(msg: dict) -> None:
    """
    Route a single decoded JSON-RPC message.
    Notifications (no 'id') are handled but never produce a response.
    """
    method = msg.get("method", "")
    req_id = msg.get("id")          # None for notifications
    params = msg.get("params") or {}
    is_notification = req_id is None and "method" in msg

    log.debug("RECV ← method=%s id=%s", method, req_id)

    # ── initialize ────────────────────────────────────────────────────────────
    if method == "initialize":
        client_version = params.get("protocolVersion", "")
        log.info("Client protocolVersion: %s", client_version)
        send(
            make_response(
                req_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "serverInfo": SERVER_INFO,
                    "capabilities": SERVER_CAPABILITIES,
                    "instructions": (
                        "This server exposes a single 'hello_world' tool "
                        "that returns a greeting message."
                    ),
                },
            )
        )
        return

    # ── initialized (notification – no response expected) ─────────────────────
    if method == "initialized":
        log.info("Session initialised – ready to serve requests.")
        return

    # ── ping ──────────────────────────────────────────────────────────────────
    if method == "ping":
        send(make_response(req_id, {}))
        return

    # ── tools/list ───────────────────────────────────────────────────────────
    if method == "tools/list":
        send(make_response(req_id, {"tools": TOOLS}))
        return

    # ── tools/call ───────────────────────────────────────────────────────────
    if method == "tools/call":
        tool_name = params.get("name", "")
        if tool_name == "hello_world":
            result = tool_hello_world(params)
            send(make_response(req_id, result))
        else:
            send(
                make_error(
                    req_id,
                    -32601,
                    f"Unknown tool: '{tool_name}'",
                )
            )
        return

    # ── unknown method ────────────────────────────────────────────────────────
    if not is_notification:
        send(make_error(req_id, -32601, f"Method not found: '{method}'"))


# ── main read loop ────────────────────────────────────────────────────────────
def run() -> None:
    """
    Read newline-delimited JSON from stdin and dispatch each message.
    Exits cleanly on EOF (client closed the pipe).
    """
    log.info("hello-world MCP server started (stdio transport).")
    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            msg = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            log.error("JSON parse error: %s", exc)
            send(make_error(None, -32700, f"Parse error: {exc}"))
            continue
        try:
            handle(msg)
        except Exception as exc:  # noqa: BLE001
            log.exception("Internal error while handling message")
            req_id = msg.get("id")
            if req_id is not None:
                send(make_error(req_id, -32603, f"Internal error: {exc}"))

    log.info("stdin closed – server shutting down.")


if __name__ == "__main__":
    run()
