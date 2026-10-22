"""零依赖的 MCP Streamable HTTP 客户端。

麦当劳中国 MCP Server（https://mcp.mcd.cn）采用 MCP 规范的 Streamable HTTP 传输：
  - POST 单端点，请求体为 JSON-RPC 2.0
  - 响应可能是 application/json，也可能是 text/event-stream（SSE）
  - initialize 后服务端可能下发 Mcp-Session-Id，后续请求需回传

本模块只依赖标准库，方便评委 clone 后直接运行，无需 pip install。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional

from .config import Settings


class MCPError(RuntimeError):
    """MCP 调用失败。"""


class MCPAuthError(MCPError):
    """401：Token 无效 / 过期 / 未携带。"""


class MCPRateLimitError(MCPError):
    """429：超过 600 次/分钟限流。"""


class MCPClient:
    def __init__(self, settings: Settings, max_retries: int = 3):
        self.settings = settings
        self.max_retries = max_retries
        self.session_id: Optional[str] = None
        self._next_id = 0
        self._initialized = False
        self.server_info: Dict[str, Any] = {}

    # ------------------------------------------------------------------ 基础
    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.settings.token}",
            "User-Agent": "growth-time-to-mcd/1.0",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self._initialized:
            headers["MCP-Protocol-Version"] = self.settings.protocol_version
        return headers

    def _post(self, payload: Dict[str, Any], expect_response: bool = True) -> Optional[Dict[str, Any]]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        want_id = payload.get("id")
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(
                self.settings.url,
                data=data,
                headers=self._headers(),
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.settings.timeout) as response:
                    if response.headers.get("Mcp-Session-Id"):
                        self.session_id = response.headers["Mcp-Session-Id"]
                    if not expect_response:
                        response.read()
                        return None
                    return self._read_body(response, want_id)

            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")[:500]
                if exc.code in (401, 403):
                    # 实测：Token 无效/过期返回 401（当前鉴权码不存在或已失效!）；
                    # 缺 Token 时服务端返回 403（{"code":"0001"}）。统一按鉴权问题处理，
                    # 403 也可能是该工具不在账号权限范围内，故错误信息同时覆盖两种情况。
                    detail = body.strip() or "无响应体"
                    raise MCPAuthError(
                        f"{exc.code} 未授权：MCD_MCP_TOKEN 无效、已过期，或该工具不在账号权限范围内。"
                        f"服务端返回：{detail}。"
                        "请到 https://open.mcd.cn/mcp 控制台重新获取 Token。"
                    ) from exc
                if exc.code == 429:
                    wait = self._retry_after(exc) or (2 ** attempt)
                    if attempt < self.max_retries:
                        time.sleep(min(wait, 10))
                        last_error = MCPRateLimitError(f"429 限流，已等待 {wait}s 后重试")
                        continue
                    raise MCPRateLimitError(
                        "429 触发限流（单 Token 上限 600 次/分钟）。请降低调用频率后重试。"
                    ) from exc
                if 500 <= exc.code < 600 and attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 8))
                    last_error = MCPError(f"{exc.code} 服务端错误：{body}")
                    continue
                raise MCPError(f"HTTP {exc.code}：{body}") from exc

            except urllib.error.URLError as exc:
                raise MCPError(
                    f"网络不可达：{exc.reason}。请检查网络代理 / 是否能访问 {self.settings.url}"
                ) from exc

        raise last_error or MCPError("请求失败")

    @staticmethod
    def _retry_after(exc: urllib.error.HTTPError) -> Optional[int]:
        try:
            return int(exc.headers.get("Retry-After", ""))
        except (TypeError, ValueError):
            return None

    def _read_body(self, response, want_id: Any) -> Optional[Dict[str, Any]]:
        content_type = response.headers.get("Content-Type", "")
        if "text/event-stream" in content_type:
            return self._read_sse(response, want_id)
        body = response.read().decode("utf-8", "replace").strip()
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise MCPError(f"响应不是合法 JSON：{body[:300]}") from exc

    def _read_sse(self, response, want_id: Any) -> Optional[Dict[str, Any]]:
        buffer: List[str] = []
        for raw in response:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line or line.startswith(":"):
                if buffer:
                    message = self._decode_event(buffer)
                    buffer = []
                    if message is not None and self._is_answer(message, want_id):
                        return message
                continue
            if line.startswith("data:"):
                buffer.append(line[5:].lstrip())
        if buffer:
            message = self._decode_event(buffer)
            if message is not None and self._is_answer(message, want_id):
                return message
        return None

    @staticmethod
    def _decode_event(lines: Iterable[str]) -> Optional[Dict[str, Any]]:
        joined = "\n".join(lines).strip()
        if not joined or joined == "[DONE]":
            return None
        try:
            parsed = json.loads(joined)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _is_answer(message: Dict[str, Any], want_id: Any) -> bool:
        if "result" in message or "error" in message:
            return want_id is None or message.get("id") == want_id
        return False

    def _request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        message = self._post(payload)
        if message is None:
            raise MCPError(f"{method} 未返回结果（空响应）")
        if "error" in message:
            error = message["error"] or {}
            raise MCPError(f"{method} 调用失败：[{error.get('code')}] {error.get('message')}")
        return message.get("result") or {}

    # ------------------------------------------------------------ MCP 生命周期
    def initialize(self) -> Dict[str, Any]:
        result = self._request(
            "initialize",
            {
                "protocolVersion": self.settings.protocol_version,
                "capabilities": {"roots": {"listChanged": False}},
                "clientInfo": {"name": "growth-time-to-mcd", "version": "1.0.0"},
            },
        )
        self._initialized = True
        self.server_info = result.get("serverInfo", {}) or {}
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_response=False)
        return result

    def ensure_ready(self) -> "MCPClient":
        if not self.settings.token:
            raise MCPAuthError(
                "缺少 MCD_MCP_TOKEN。请先到 https://open.mcd.cn/mcp 申请 Token，"
                "然后 export MCD_MCP_TOKEN=... 或写入 .env"
            )
        if not self._initialized:
            self.initialize()
        return self

    def list_tools(self) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params)
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        if result.get("isError"):
            texts = [c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text"]
            raise MCPError(f"工具 {name} 返回错误：{' '.join(texts)[:400]}")
        return result

    # ------------------------------------------------------------------ 便捷封装
    def call_json(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        """调用工具并把 content 里的文本尽量解析成 JSON。"""
        result = self.call_tool(name, arguments)
        return extract_payload(result)

    def __enter__(self) -> "MCPClient":
        return self.ensure_ready()

    def __exit__(self, *exc_info) -> None:
        return None


def extract_payload(result: Dict[str, Any]) -> Any:
    """把 tools/call 的 content 归一成 Python 对象。

    MCP 工具通常返回 [{type: "text", text: "<json 或纯文本>"}]，
    个别工具会带 structuredContent。优先用结构化结果，其次解析文本。
    """
    if isinstance(result.get("structuredContent"), (dict, list)):
        return result["structuredContent"]

    chunks: List[str] = []
    for item in result.get("content") or []:
        if item.get("type") == "text":
            chunks.append(item.get("text") or "")
    text = "\n".join(chunks).strip()
    if not text:
        return {}

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 有些服务端会把 JSON 包在 ```json 代码块里
    if "```" in text:
        for block in text.split("```"):
            block = block.strip()
            if block.startswith("json"):
                block = block[4:].strip()
            try:
                return json.loads(block)
            except json.JSONDecodeError:
                continue
    return {"text": text}
