"""
Protocol-level tests for the hello-world MCP server.

Strategy: drive the server as a subprocess via its stdin/stdout interface,
sending real JSON-RPC 2.0 messages and asserting over the responses.
No mocking of internal functions – this exercises the full message loop.
"""

import json
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

SERVER_PATH = Path(__file__).parent / "server.py"


# ── helpers ───────────────────────────────────────────────────────────────────

def _start_server():
    """Spawn the server as a child process."""
    return subprocess.Popen(
        [sys.executable, str(SERVER_PATH)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,          # line-buffered
    )


def _send(proc, obj: dict) -> None:
    line = json.dumps(obj) + "\n"
    proc.stdin.write(line)
    proc.stdin.flush()


def _recv(proc, timeout: float = 3.0) -> dict:
    """Read one JSON line from the server's stdout."""
    proc.stdout.readline.__func__  # make sure it's a real file
    import select
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready:
        raise TimeoutError("Server did not respond within timeout.")
    line = proc.stdout.readline()
    return json.loads(line)


def _do_handshake(proc) -> dict:
    """Perform the mandatory initialize / initialized handshake."""
    _send(proc, {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "clientInfo": {"name": "test-client", "version": "0.0.1"},
            "capabilities": {},
        },
    })
    resp = _recv(proc)
    # initialized notification – no response expected
    _send(proc, {"jsonrpc": "2.0", "method": "initialized"})
    return resp


# ── test cases ────────────────────────────────────────────────────────────────

class TestMCPProtocol(unittest.TestCase):

    def setUp(self):
        self.proc = _start_server()
        time.sleep(0.05)   # let the process warm up

    def tearDown(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=3)

    # ── 1. initialize handshake ───────────────────────────────────────────────
    def test_initialize_response_shape(self):
        resp = _do_handshake(self.proc)
        self.assertEqual(resp["jsonrpc"], "2.0")
        self.assertEqual(resp["id"], 1)
        result = resp["result"]
        self.assertEqual(result["protocolVersion"], "2025-11-25")
        self.assertIn("serverInfo", result)
        self.assertEqual(result["serverInfo"]["name"], "hello-world-mcp")
        self.assertIn("capabilities", result)
        self.assertIn("tools", result["capabilities"])

    # ── 2. tools/list ─────────────────────────────────────────────────────────
    def test_tools_list_returns_hello_world(self):
        _do_handshake(self.proc)
        _send(self.proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        resp = _recv(self.proc)
        self.assertEqual(resp["id"], 2)
        tools = resp["result"]["tools"]
        self.assertEqual(len(tools), 1)
        tool = tools[0]
        self.assertEqual(tool["name"], "hello_world")
        self.assertIn("description", tool)
        self.assertIn("inputSchema", tool)
        self.assertEqual(tool["inputSchema"]["type"], "object")

    # ── 3. tools/call – default greeting ─────────────────────────────────────
    def test_tools_call_default_greeting(self):
        _do_handshake(self.proc)
        _send(self.proc, {
            "jsonrpc": "2.0", "id": 3,
            "method": "tools/call",
            "params": {"name": "hello_world", "arguments": {}},
        })
        resp = _recv(self.proc)
        self.assertEqual(resp["id"], 3)
        result = resp["result"]
        self.assertFalse(result.get("isError", True))
        content = result["content"]
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]["type"], "text")
        self.assertIn("Hello, World!", content[0]["text"])

    # ── 4. tools/call – personalised greeting ────────────────────────────────
    def test_tools_call_named_greeting(self):
        _do_handshake(self.proc)
        _send(self.proc, {
            "jsonrpc": "2.0", "id": 4,
            "method": "tools/call",
            "params": {"name": "hello_world", "arguments": {"name": "Alice"}},
        })
        resp = _recv(self.proc)
        self.assertEqual(resp["id"], 4)
        text = resp["result"]["content"][0]["text"]
        self.assertIn("Alice", text)

    # ── 5. tools/call – empty string name falls back to World ────────────────
    def test_tools_call_empty_name_fallback(self):
        _do_handshake(self.proc)
        _send(self.proc, {
            "jsonrpc": "2.0", "id": 5,
            "method": "tools/call",
            "params": {"name": "hello_world", "arguments": {"name": "   "}},
        })
        resp = _recv(self.proc)
        text = resp["result"]["content"][0]["text"]
        self.assertIn("World", text)

    # ── 6. tools/call – unknown tool returns method-not-found error ───────────
    def test_tools_call_unknown_tool(self):
        _do_handshake(self.proc)
        _send(self.proc, {
            "jsonrpc": "2.0", "id": 6,
            "method": "tools/call",
            "params": {"name": "does_not_exist", "arguments": {}},
        })
        resp = _recv(self.proc)
        self.assertEqual(resp["id"], 6)
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32601)

    # ── 7. unknown method returns MethodNotFound ──────────────────────────────
    def test_unknown_method_returns_error(self):
        _do_handshake(self.proc)
        _send(self.proc, {
            "jsonrpc": "2.0", "id": 7,
            "method": "no_such_method",
        })
        resp = _recv(self.proc)
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32601)

    # ── 8. malformed JSON returns parse error ─────────────────────────────────
    def test_parse_error_on_bad_json(self):
        _do_handshake(self.proc)
        self.proc.stdin.write("{ this is not json }\n")
        self.proc.stdin.flush()
        resp = _recv(self.proc)
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32700)

    # ── 9. ping responds with empty result ───────────────────────────────────
    def test_ping(self):
        _do_handshake(self.proc)
        _send(self.proc, {"jsonrpc": "2.0", "id": 8, "method": "ping"})
        resp = _recv(self.proc)
        self.assertEqual(resp["id"], 8)
        self.assertEqual(resp["result"], {})

    # ── 10. initialized notification produces no response ────────────────────
    def test_initialized_notification_no_response(self):
        """
        After handshake, send a second 'initialized' notification followed
        immediately by a ping.  The next thing we read must be the ping reply
        (id 9), NOT a spurious response to the notification.
        """
        _do_handshake(self.proc)
        _send(self.proc, {"jsonrpc": "2.0", "method": "initialized"})   # notification
        _send(self.proc, {"jsonrpc": "2.0", "id": 9, "method": "ping"})
        resp = _recv(self.proc)
        self.assertEqual(resp.get("id"), 9)


class TestToolLogic(unittest.TestCase):
    """Unit-test the tool function directly, without a subprocess."""

    def setUp(self):
        # Add the server directory to sys.path so we can import it
        import importlib, sys
        sys.path.insert(0, str(SERVER_PATH.parent))
        self.mod = importlib.import_module("server")

    def test_default_name(self):
        result = self.mod.tool_hello_world({"arguments": {}})
        self.assertFalse(result["isError"])
        self.assertIn("World", result["content"][0]["text"])

    def test_custom_name(self):
        result = self.mod.tool_hello_world({"arguments": {"name": "Bob"}})
        self.assertFalse(result["isError"])
        self.assertIn("Bob", result["content"][0]["text"])

    def test_non_string_name_returns_error(self):
        result = self.mod.tool_hello_world({"arguments": {"name": 42}})
        self.assertTrue(result["isError"])

    def test_whitespace_only_name_falls_back(self):
        result = self.mod.tool_hello_world({"arguments": {"name": "   "}})
        self.assertFalse(result["isError"])
        self.assertIn("World", result["content"][0]["text"])

    def test_missing_arguments_key(self):
        result = self.mod.tool_hello_world({})
        self.assertFalse(result["isError"])
        self.assertIn("World", result["content"][0]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
