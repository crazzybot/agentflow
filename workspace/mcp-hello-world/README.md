# hello-world MCP Server

A minimal, dependency-free reference implementation of an
[Model Context Protocol (MCP)](https://modelcontextprotocol.io) server,
written in pure Python.  
It exposes a single **`hello_world`** tool and correctly implements the full
MCP session lifecycle (initialization handshake, capability negotiation,
`tools/list`, `tools/call`).

---

## Features

| Feature | Detail |
|---|---|
| MCP spec version | **2025-11-25** |
| Transport | **stdio** (newline-delimited JSON-RPC 2.0) |
| Runtime dependencies | **none** (Python stdlib only) |
| Tool | `hello_world` – returns a greeting message |

---

## Project layout

```
mcp-hello-world/
├── server.py          ← MCP server (entry point)
├── test_server.py     ← Protocol-level + unit tests
├── requirements.txt   ← Dev deps (pytest)
└── README.md
```

---

## Quick start

### 1. Clone / copy the files

```bash
git clone <repo-url> mcp-hello-world
cd mcp-hello-world
```

### 2. (Optional) create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
```

### 3. Install dev dependencies

```bash
pip install -r requirements.txt
```

### 4. Run the tests

```bash
pytest test_server.py -v
```

### 5. Interact manually via the CLI

Because the server uses **stdio transport** you drive it by piping
newline-delimited JSON-RPC 2.0 messages into its stdin.

```bash
python server.py
```

Paste the following lines one at a time (or pipe a file):

```jsonc
// Step 1 – initialize
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","clientInfo":{"name":"manual-test","version":"0.0.1"},"capabilities":{}}}

// Step 2 – confirm readiness (notification, no response)
{"jsonrpc":"2.0","method":"initialized"}

// Step 3 – list available tools
{"jsonrpc":"2.0","id":2,"method":"tools/list"}

// Step 4 – call hello_world (default name)
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"hello_world","arguments":{}}}

// Step 5 – call hello_world with a custom name
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"hello_world","arguments":{"name":"Alice"}}}
```

Example response for step 5:

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "Hello, Alice! 👋  Welcome to the Model Context Protocol."
      }
    ],
    "isError": false
  }
}
```

---

## Registering with Claude Desktop

Add the following block to your `claude_desktop_config.json`
(`~/Library/Application Support/Claude/` on macOS):

```json
{
  "mcpServers": {
    "hello-world": {
      "command": "python",
      "args": ["/absolute/path/to/mcp-hello-world/server.py"]
    }
  }
}
```

Restart Claude Desktop.  The **`hello_world`** tool will appear in the
tool picker automatically.

---

## Debugging with MCP Inspector

```bash
npx @modelcontextprotocol/inspector python /absolute/path/to/server.py
```

Open the printed URL in your browser to inspect requests and responses
interactively.

---

## MCP session lifecycle (implemented)

```
Client                          Server
  │                               │
  │──── initialize ──────────────▶│  protocolVersion, clientInfo, capabilities
  │◀─── initialize response ──────│  protocolVersion, serverInfo, capabilities
  │──── initialized (notif) ─────▶│  (no response)
  │                               │
  │──── tools/list ──────────────▶│
  │◀─── tools/list response ──────│  [{name, description, inputSchema}]
  │                               │
  │──── tools/call ──────────────▶│  {name: "hello_world", arguments: {…}}
  │◀─── tools/call response ──────│  {content: [{type:"text", text:"…"}]}
```

---

## Error handling

| Scenario | JSON-RPC error code |
|---|---|
| Malformed JSON | `-32700` Parse error |
| Unknown method | `-32601` Method not found |
| Unknown tool name | `-32601` Method not found |
| Internal server error | `-32603` Internal error |
| Invalid `name` type | Returned as `isError: true` inside the tool result |

---

## Extending the server

To add a new tool:

1. Append a tool definition to the `TOOLS` list in `server.py`.
2. Add a handler function `tool_<name>(params) -> dict`.
3. Add a branch in the `tools/call` dispatch block.

```python
# Example – add an 'echo' tool
TOOLS.append({
    "name": "echo",
    "description": "Echoes back the provided text.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to echo."}
        },
        "required": ["text"],
    },
})

def tool_echo(params: dict) -> dict:
    text = (params.get("arguments") or {}).get("text", "")
    return {"content": [{"type": "text", "text": text}], "isError": False}
```

---

## License

MIT
