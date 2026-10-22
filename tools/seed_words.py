#!/usr/bin/env python
"""把 CET-4 高频词表写入 SQLite 的 wordbank 表（"把单词表放到数据库里")。

用法：
    python tools/seed_words.py                 # 写入默认库
    python tools/seed_words.py --db data/demo.db
    python tools/seed_words.py --bank data/wordbank.json

仅做 INSERT OR IGNORE，可重复执行，不会覆盖已有记录。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description="把词表写入数据库 wordbank 表")
    ap.add_argument("--db", help="SQLite 路径，默认自动挑选")
    ap.add_argument("--bank", default=str(ROOT / "data" / "wordbank.json"), help="词表 JSON")
    args = ap.parse_args()

    db_path = args.db
    if not db_path:
        import os

        env = os.environ.get("T2M_DB")
        db_path = env or str(ROOT / "data" / "records.db")

    bank = Path(args.bank)
    if not bank.is_file():
        raise SystemExit(f"找不到词表：{bank}")
    payload = json.loads(bank.read_text(encoding="utf-8"))
    words = payload.get("words", [])
    if not words:
        raise SystemExit("词表为空")

    sys.path.insert(0, str(ROOT / "src"))
    from time_to_mcd.store import RecordStore  # noqa: E402

    store = RecordStore(Path(db_path))
    added = store.seed_wordbank(words)

    print(f"词表来源 : {bank}")
    print(f"目标库   : {db_path}")
    print(f"词表总数 : {len(words)}")
    print(f"本次新增 : {added} 条（已存在则跳过）")
    print(f"库内累计 : {store.wordbank_size()} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
