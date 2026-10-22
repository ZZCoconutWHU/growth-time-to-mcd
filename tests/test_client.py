"""MCP 客户端协议测试：用本地 HTTP 桩验证握手、SSE 解析、会话与错误分型。

不连外网，也不依赖真实 Token，固定住客户端的协议行为。
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from time_to_mcd.config import Settings  # noqa: E402
from time_to_mcd.mcp_client import (  # noqa: E402
    MCPAuthError,
    MCPClient,
    MCPError,
    MCPRateLimitError,
)


class _StubHandler(BaseHTTPRequestHandler):
    """按 mode 返回不同响应，模拟真实服务端的几种关键行为。"""

    mode = "sse"
    seen_headers: list = []
    seen_bodies: list = []

    def log_message(self, *args):  # 静默
        pass

    def _write(self, status: int, body: bytes, content_type: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).seen_headers.append(dict(self.headers))
        type(self).seen_bodies.append(payload)
        method = payload.get("method")

        if method == "notifications/initialized":
            self._write(202, b"", "application/json")
            return

        if type(self).mode == "403":
            self._write(403, b'{"code":"0001","msg":"x"}', "application/json")
            return
        if type(self).mode == "401":
            self._write(401, "当前鉴权码不存在或已失效!".encode("utf-8"), "text/plain")
            return
        if type(self).mode == "429":
            self._write(429, b"rate limited", "text/plain", {"Retry-After": "0"})
            return
        if type(self).mode == "500":
            self._write(500, b"boom", "text/plain")
            return

        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "stub-mcd", "version": "9.9"}}
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "query-nearby-stores",
                        "description": "查询附近门店",
                        "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string"}}, "required": ["keyword"]},
                    }
                ]
            }
        else:
            result = {"content": [{"type": "text", "text": "{\"ok\": true}"}]}

        message = {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}

        if type(self).mode == "sse":
            body = b"data: " + json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n\n"
            self._write(200, body, "text/event-stream", {"Mcp-Session-Id": "sess-abc"})
        else:
            self._write(200, json.dumps(message).encode("utf-8"), "application/json")


class ClientProtocolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _StubHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _StubHandler.mode = "sse"
        _StubHandler.seen_headers = []
        _StubHandler.seen_bodies = []

    def _client(self, mode: str, token: str = "stub-token", retries: int = 0) -> MCPClient:
        _StubHandler.mode = mode
        settings = Settings(url=f"http://127.0.0.1:{self.port}", token=token)
        return MCPClient(settings, max_retries=retries)

    def test_sse_initialize_parses_result_and_session(self):
        client = self._client("sse")
        info = client.initialize()
        self.assertEqual(info["serverInfo"]["name"], "stub-mcd")
        self.assertEqual(client.session_id, "sess-abc")
        # 握手后应带上协议版本与会话 id
        last_headers = {k.lower(): v for k, v in _StubHandler.seen_headers[-1].items()}
        self.assertEqual(last_headers.get("mcp-session-id"), "sess-abc")
        self.assertEqual(last_headers.get("mcp-protocol-version"), "2025-06-18")

    def test_initialized_notification_sent(self):
        client = self._client("sse")
        client.initialize()
        methods = [body.get("method") for body in _StubHandler.seen_bodies]
        self.assertIn("notifications/initialized", methods)

    def test_json_response_mode(self):
        client = self._client("json")
        tools = client.list_tools()
        self.assertEqual(tools[0]["name"], "query-nearby-stores")

    def test_authorization_header(self):
        client = self._client("json", token="abc123")
        client.initialize()
        auth = {k.lower(): v for k, v in _StubHandler.seen_headers[0].items()}.get("authorization")
        self.assertEqual(auth, "Bearer abc123")

    def test_403_is_auth_error(self):
        # 服务端在缺 Token 或工具无权限时返回 403
        client = self._client("403")
        with self.assertRaises(MCPAuthError) as ctx:
            client.ensure_ready()
        self.assertIn("403", str(ctx.exception))

    def test_empty_token_fails_before_request(self):
        client = self._client("json", token="")
        with self.assertRaises(MCPAuthError) as ctx:
            client.ensure_ready()
        self.assertIn("MCD_MCP_TOKEN", str(ctx.exception))
        self.assertEqual(_StubHandler.seen_bodies, [])

    def test_401_with_bad_token_is_auth_error(self):
        client = self._client("401")
        with self.assertRaises(MCPAuthError) as ctx:
            client.ensure_ready()
        self.assertIn("401", str(ctx.exception))
        self.assertIn("已失效", str(ctx.exception))

    def test_429_is_rate_limit_error(self):
        client = self._client("429", retries=0)
        with self.assertRaises(MCPRateLimitError):
            client.ensure_ready()

    def test_500_is_generic_error(self):
        client = self._client("500", retries=0)
        with self.assertRaises(MCPError) as ctx:
            client.ensure_ready()
        self.assertNotIsInstance(ctx.exception, MCPAuthError)

    def test_tool_call_extracts_json_content(self):
        client = self._client("json")
        client.ensure_ready()
        payload = client.call_json("query-nearby-stores", {"keyword": "静安寺"})
        self.assertEqual(payload, {"ok": True})


if __name__ == "__main__":
    unittest.main()
