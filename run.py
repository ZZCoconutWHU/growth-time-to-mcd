#!/usr/bin/env python
"""免安装启动器：python run.py --mock plan --address ... --items ...

作用只有一个：把 src/ 加入 sys.path，让评委不用 pip install 就能跑。
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))


def _cmd_web(argv: list[str]) -> int:
    import os

    ap = argparse.ArgumentParser(prog="run.py web", description="启动本地 Web 服务（前端交互入口）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--demo", action="store_true", help="使用内置演示库 data/demo.db（指标齐全，适合评审演示）")
    ap.add_argument("--db", default=None, help="自定义 SQLite 路径（覆盖默认 data/records.db）")
    args = ap.parse_args(argv)

    # 必须先设置 T2M_DB，再导入 web_app：其 _DB_PATH 在模块导入时读取环境变量
    if args.db:
        os.environ["T2M_DB"] = args.db
    elif args.demo:
        os.environ["T2M_DB"] = str(ROOT / "data" / "demo.db")
        # 演示模式连 MCP 也用假数据：不把真实账号里的订单/门店投到屏幕上
        os.environ["T2M_MOCK"] = "1"

    from time_to_mcd.web_app import run

    run(args.host, args.port)
    return 0


if __name__ == "__main__":
    # `python run.py web` 走本地服务；其余子命令交给 CLI
    cli_argv = sys.argv[1:]
    if cli_argv and cli_argv[0] == "web":
        raise SystemExit(_cmd_web(cli_argv[1:]))
    from time_to_mcd.cli import main  # noqa: E402

    raise SystemExit(main())
