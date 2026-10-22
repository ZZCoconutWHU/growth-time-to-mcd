"""麦麦成长时刻 —— 配置读取。

只从环境变量 / .env 读取凭证，绝不把 Token 写进仓库。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_URL = "https://mcp.mcd.cn"
DEFAULT_PROTOCOL = "2025-06-18"

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """极简 .env 解析：仅支持 KEY=VALUE 与 # 注释，不覆盖已有环境变量。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Settings:
    url: str
    token: str
    protocol_version: str = DEFAULT_PROTOCOL
    timeout: float = 30.0
    mock: bool = False
    tmap_key: str = ""

    @property
    def masked_token(self) -> str:
        if not self.token:
            return "(未设置)"
        if len(self.token) <= 8:
            return "*" * len(self.token)
        return f"{self.token[:4]}{'*' * 8}{self.token[-4:]}"


def load_settings(mock: bool = False) -> Settings:
    _load_dotenv(_REPO_ROOT / ".env")
    return Settings(
        url=os.environ.get("MCD_MCP_URL", DEFAULT_URL).rstrip("/"),
        token=os.environ.get("MCD_MCP_TOKEN", "").strip(),
        protocol_version=os.environ.get("MCD_MCP_PROTOCOL", DEFAULT_PROTOCOL),
        timeout=float(os.environ.get("MCD_MCP_TIMEOUT", "30")),
        mock=mock,
        tmap_key=os.environ.get("T2M_TMAP_KEY", "").strip(),
    )
