"""本地成长档案：SQLite 记录每一餐、每一步、每一个单词。

设计原则：
  - 只存在本地（默认 `<repo>/data/records.db`），不上传、不联网；
  - 所有统计都由 SQL 聚合得出，不做内存累加，避免多处写入导致数字对不上；
  - 时间统一存 ISO 字符串，便于人眼直接读库。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]


def default_db_path() -> Path:
    override = os.environ.get("T2M_DB")
    if override:
        return Path(override)
    return _REPO_ROOT / "data" / "records.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    height_cm   REAL NOT NULL,
    weight_kg   REAL NOT NULL,
    age         INTEGER NOT NULL,
    sex         TEXT NOT NULL,
    activity    TEXT NOT NULL,
    goal        TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    store       TEXT,
    meal_type   TEXT,
    items_json  TEXT NOT NULL,
    kcal        REAL NOT NULL DEFAULT 0,
    protein     REAL NOT NULL DEFAULT 0,
    fat         REAL NOT NULL DEFAULT 0,
    carb        REAL NOT NULL DEFAULT 0,
    sodium      REAL NOT NULL DEFAULT 0,
    target_kcal REAL,
    on_target   INTEGER NOT NULL DEFAULT 0,
    source      TEXT,
    ref_id      TEXT,
    uid         TEXT
);

CREATE TABLE IF NOT EXISTS commutes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    store       TEXT,
    mode        TEXT NOT NULL,
    distance_m  REAL NOT NULL,
    minutes     REAL NOT NULL,
    kcal        REAL NOT NULL,
    source      TEXT,
    ref_id      TEXT,
    uid         TEXT
);

CREATE TABLE IF NOT EXISTS word_progress (
    word        TEXT PRIMARY KEY,
    reps        INTEGER NOT NULL DEFAULT 0,
    ease        REAL NOT NULL DEFAULT 2.5,
    interval_d  INTEGER NOT NULL DEFAULT 1,
    due_date    TEXT NOT NULL,
    correct     INTEGER NOT NULL DEFAULT 0,
    wrong       INTEGER NOT NULL DEFAULT 0,
    last_ts     TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    duration_min REAL NOT NULL DEFAULT 0,
    words       INTEGER NOT NULL DEFAULT 0,
    correct     INTEGER NOT NULL DEFAULT 0,
    kcal_burned REAL NOT NULL DEFAULT 0,
    meta_json   TEXT,
    uid         TEXT
);

CREATE TABLE IF NOT EXISTS badges (
    code        TEXT PRIMARY KEY,
    unlocked_at TEXT NOT NULL
);

-- 价格锚点：某个商品「卖多少钱、属于哪一类、营养是多少」。
-- 麦当劳营养库只有营养没有价格，而订单明细只有价格没有营养，
-- 两边靠商品名对上之后沉淀到这里；以后遇到营养库里查不到的新品，
-- 就按「同类别 + 价格最接近」找这里的一条来估算。
CREATE TABLE IF NOT EXISTS food_anchors (
    name        TEXT PRIMARY KEY,
    category    TEXT NOT NULL DEFAULT "",
    price       REAL NOT NULL DEFAULT 0,
    kcal        REAL NOT NULL DEFAULT 0,
    protein     REAL NOT NULL DEFAULT 0,
    fat         REAL NOT NULL DEFAULT 0,
    carb        REAL NOT NULL DEFAULT 0,
    sodium      REAL NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT "",
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wordbank (
    en          TEXT PRIMARY KEY,
    zh          TEXT NOT NULL,
    pos         TEXT NOT NULL DEFAULT '',
    level       TEXT NOT NULL DEFAULT 'CET4'
);

-- 同步元数据：手机/电脑之间对账用（上次同步时间、最后同步的设备）。
CREATE TABLE IF NOT EXISTS sync_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
"""

TODAY = lambda: date.today().isoformat()  # noqa: E731


def _parse_ts(raw: Any) -> Optional[datetime]:
    """同步包里的时间字段可能是 None / 带空格 / 带时区，统一解析成 datetime。"""
    text = str(raw or "").strip()
    if not text:
        return None
    text = text.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19] if fmt != "%Y-%m-%d" else text[:10], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


@dataclass
class Profile:
    height_cm: float
    weight_kg: float
    age: int
    sex: str = "male"
    activity: str = "light"
    goal: str = "lose"

    @property
    def bmi(self) -> float:
        return self.weight_kg / ((self.height_cm / 100) ** 2)


@dataclass
class WordResult:
    word: str
    correct: bool
    reps: int
    interval_d: int
    due_date: str
    mastered: bool


class RecordStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """给旧库补新列。

        `CREATE TABLE IF NOT EXISTS` 对已存在的表不会补列，
        直接用新字段会报 "no such column"，所以这里显式 ALTER。
        """
        wanted = {
            "meals": {"source": "TEXT", "ref_id": "TEXT", "uid": "TEXT"},
            # 通勤消耗要能挂到「哪一单」上：同一单重复上报时不再重复累加卡路里
            "commutes": {"source": "TEXT", "ref_id": "TEXT", "uid": "TEXT"},
            "sessions": {"uid": "TEXT"},
        }
        with self._conn() as conn:
            for table, columns in wanted.items():
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                for column, ddl in columns.items():
                    if column not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            self._ensure_uid_indexes(conn)

    @staticmethod
    def _ensure_uid_indexes(conn: sqlite3.Connection) -> None:
        """给 uid 建唯一索引——手机补传重试时靠它保证一条记录只落一次。

        唯一索引对 NULL 不生效，所以老数据（没有 uid）照样能继续写。
        """
        for table in ("meals", "commutes", "sessions"):
            conn.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_uid ON {table}(uid) WHERE uid IS NOT NULL AND uid <> ''"
            )

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------ 档案
    def save_profile(self, profile: Profile) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO profile (id, height_cm, weight_kg, age, sex, activity, goal, updated_at)
                   VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     height_cm=excluded.height_cm, weight_kg=excluded.weight_kg, age=excluded.age,
                     sex=excluded.sex, activity=excluded.activity, goal=excluded.goal,
                     updated_at=excluded.updated_at""",
                (
                    profile.height_cm,
                    profile.weight_kg,
                    profile.age,
                    profile.sex,
                    profile.activity,
                    profile.goal,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )

    def get_profile(self) -> Optional[Profile]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
        if not row:
            return None
        return Profile(
            height_cm=row["height_cm"],
            weight_kg=row["weight_kg"],
            age=row["age"],
            sex=row["sex"],
            activity=row["activity"],
            goal=row["goal"],
        )

    # ------------------------------------------------------------------ 餐
    def log_meal(
        self,
        items: List[Dict[str, Any]],
        kcal: float,
        protein: float,
        fat: float,
        carb: float,
        sodium: float,
        target_kcal: Optional[float] = None,
        store: str = "",
        meal_type: str = "",
        on_target: Optional[bool] = None,
        source: str = "",
        ref_id: str = "",
        ts: Optional[datetime] = None,
        uid: str = "",
    ) -> int:
        if uid and self._uid_rowid("meals", uid):
            return 0
        if on_target is None:
            on_target = bool(target_kcal) and abs(kcal - target_kcal) <= target_kcal * 0.15
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO meals (ts, store, meal_type, items_json, kcal, protein, fat, carb, sodium, target_kcal, on_target, source, ref_id, uid)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    (ts or datetime.now()).isoformat(timespec="seconds"),
                    store,
                    meal_type,
                    json.dumps(items, ensure_ascii=False),
                    kcal,
                    protein,
                    fat,
                    carb,
                    sodium,
                    target_kcal,
                    1 if on_target else 0,
                    source,
                    ref_id,
                    uid or None,
                ),
            )
            return int(cur.lastrowid)

    # ------------------------------------------------------------------ 通勤
    def log_commute(
        self,
        store: str,
        mode: str,
        distance_m: float,
        minutes: float,
        kcal: float,
        source: str = "",
        ref_id: str = "",
        ts: Optional[datetime] = None,
        uid: str = "",
    ) -> int:
        """记一次通勤消耗。

        ref_id 用来把「这一趟」绑到某一单上：下单后前端每 2 分钟轮询一次，
        同一单只能计入一次卡路里，否则每轮询一次就多加一份消耗。
        """
        with self._conn() as conn:
            if uid and self._uid_rowid("commutes", uid):
                return 0
            cur = conn.execute(
                """INSERT INTO commutes (ts, store, mode, distance_m, minutes, kcal, source, ref_id, uid)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    (ts or datetime.now()).isoformat(timespec="seconds"),
                    store,
                    mode,
                    distance_m,
                    minutes,
                    kcal,
                    source,
                    ref_id,
                    uid or None,
                ),
            )
            return int(cur.lastrowid)

    # ---------------------------------------------------------- 价格锚点
    def upsert_food_anchor(
        self,
        name: str,
        category: str,
        price: float,
        kcal: float,
        protein: float,
        fat: float,
        carb: float,
        sodium: float,
        source: str = "mcp-order",
    ) -> None:
        """沉淀一条「价格 → 营养」的对应关系，供以后估算查不到的新品。"""
        name = (name or "").strip()
        if not name or price <= 0 or kcal <= 0:
            return
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO food_anchors (name, category, price, kcal, protein, fat, carb, sodium, source, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     category=excluded.category, price=excluded.price, kcal=excluded.kcal,
                     protein=excluded.protein, fat=excluded.fat, carb=excluded.carb,
                     sodium=excluded.sodium, source=excluded.source, updated_at=excluded.updated_at""",
                (
                    name,
                    category,
                    price,
                    kcal,
                    protein,
                    fat,
                    carb,
                    sodium,
                    source,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )

    def food_anchors(self, category: str = "") -> List[Dict[str, Any]]:
        with self._conn() as conn:
            if category:
                rows = conn.execute(
                    "SELECT * FROM food_anchors WHERE category = ? ORDER BY price", (category,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM food_anchors ORDER BY category, price").fetchall()
        return [dict(row) for row in rows]

    def logged_commute_refs(self) -> set:
        """已经计入过消耗的订单号（去重用）。"""
        with self._conn() as conn:
            rows = conn.execute("SELECT ref_id FROM commutes WHERE ref_id IS NOT NULL AND ref_id != ''").fetchall()
        return {row["ref_id"] for row in rows}

    # ------------------------------------------------------------------ 单词
    def word_progress(self) -> Dict[str, Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM word_progress").fetchall()
        return {row["word"]: dict(row) for row in rows}

    # ------------------------------------------------------------------ 词库
    def seed_wordbank(self, words: Iterable[Dict[str, Any]]) -> int:
        """把词表写入 wordbank 表（已存在则跳过），返回新增条数。"""
        count = 0
        with self._conn() as conn:
            for w in words:
                if not w.get("en") or not w.get("zh"):
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO wordbank (en, zh, pos, level) VALUES (?, ?, ?, ?)",
                    (w["en"], w["zh"], w.get("pos", ""), w.get("level", "CET4")),
                )
                count += cur.rowcount
        return count

    def wordbank_size(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) AS c FROM wordbank").fetchone()["c"]

    def sample_words(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT en, zh, pos, level FROM wordbank ORDER BY RANDOM() LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def due_words(self, limit: int = 50) -> List[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT word FROM word_progress WHERE due_date <= ? ORDER BY due_date ASC LIMIT ?",
                (TODAY(), limit),
            ).fetchall()
        return [row["word"] for row in rows]

    def record_word(self, word: str, correct: bool) -> WordResult:
        """简化版 SM-2：答对拉长间隔，答错回到 1 天。"""
        today = date.today()
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM word_progress WHERE word = ?", (word,)).fetchone()
            if row:
                reps, ease, interval, ok, bad = row["reps"], row["ease"], row["interval_d"], row["correct"], row["wrong"]
            else:
                reps, ease, interval, ok, bad = 0, 2.5, 1, 0, 0

            if correct:
                reps += 1
                ok += 1
                ease = min(3.0, ease + 0.1)
                if reps == 1:
                    interval = 1
                elif reps == 2:
                    interval = 3
                else:
                    interval = max(1, round(interval * ease))
            else:
                reps = 0
                bad += 1
                ease = max(1.3, ease - 0.2)
                interval = 1

            due = (today + timedelta(days=interval)).isoformat()
            conn.execute(
                """INSERT INTO word_progress (word, reps, ease, interval_d, due_date, correct, wrong, last_ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(word) DO UPDATE SET
                     reps=excluded.reps, ease=excluded.ease, interval_d=excluded.interval_d,
                     due_date=excluded.due_date, correct=excluded.correct, wrong=excluded.wrong,
                     last_ts=excluded.last_ts""",
                (word, reps, ease, interval, due, ok, bad, datetime.now().isoformat(timespec="seconds")),
            )
        return WordResult(word, correct, reps, interval, due, mastered=reps >= 3)

    # ------------------------------------------------------------------ 会话
    def log_session(
        self,
        kind: str,
        duration_min: float = 0,
        words: int = 0,
        correct: int = 0,
        kcal_burned: float = 0,
        meta: Optional[Dict[str, Any]] = None,
        ts: Optional[datetime] = None,
        uid: str = "",
    ) -> int:
        with self._conn() as conn:
            if uid and self._uid_rowid("sessions", uid):
                return 0
            cur = conn.execute(
                """INSERT INTO sessions (ts, kind, duration_min, words, correct, kcal_burned, meta_json, uid)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    (ts or datetime.now()).isoformat(timespec="seconds"),
                    kind,
                    duration_min,
                    words,
                    correct,
                    kcal_burned,
                    json.dumps(meta or {}, ensure_ascii=False),
                    uid or None,
                ),
            )
            return int(cur.lastrowid)

    # ------------------------------------------------------------------ 统计
    def stats(self) -> Dict[str, Any]:
        with self._conn() as conn:
            meal = conn.execute(
                """SELECT COUNT(*) AS total,
                          COALESCE(SUM(on_target), 0) AS on_target,
                          COALESCE(SUM(kcal), 0) AS kcal,
                          COALESCE(SUM(protein), 0) AS protein,
                          COALESCE(SUM(fat), 0) AS fat,
                          COALESCE(SUM(carb), 0) AS carb,
                          COALESCE(SUM(sodium), 0) AS sodium
                   FROM meals"""
            ).fetchone()
            commute = conn.execute(
                "SELECT COUNT(*) AS trips, COALESCE(SUM(distance_m), 0) AS meters, COALESCE(SUM(kcal), 0) AS kcal, COALESCE(SUM(minutes), 0) AS minutes FROM commutes"
            ).fetchone()
            words = conn.execute(
                """SELECT COUNT(*) AS studied,
                          COALESCE(SUM(correct), 0) AS correct,
                          COALESCE(SUM(wrong), 0) AS wrong,
                          COALESCE(SUM(CASE WHEN reps >= 3 THEN 1 ELSE 0 END), 0) AS mastered
                   FROM word_progress"""
            ).fetchone()
            session = conn.execute(
                """SELECT COUNT(*) AS sessions,
                          COALESCE(SUM(duration_min), 0) AS minutes,
                          COALESCE(SUM(kcal_burned), 0) AS kcal,
                          COALESCE(SUM(CASE WHEN kind = 'words' THEN duration_min ELSE 0 END), 0) AS words_min,
                          COALESCE(SUM(CASE WHEN kind = 'wait' THEN duration_min ELSE 0 END), 0) AS wait_min
                   FROM sessions"""
            ).fetchone()
            active_days = conn.execute(
                """SELECT COUNT(DISTINCT day) AS days FROM (
                       SELECT substr(ts, 1, 10) AS day FROM meals
                       UNION SELECT substr(ts, 1, 10) FROM commutes
                       UNION SELECT substr(ts, 1, 10) FROM sessions
                   )"""
            ).fetchone()

        total_words = (words["correct"] or 0) + (words["wrong"] or 0)
        return {
            "meals_total": meal["total"],
            "meals_on_target": meal["on_target"],
            "kcal_in": round(meal["kcal"], 1),
            "protein_in": round(meal["protein"], 1),
            "fat_in": round(meal["fat"], 1),
            "carb_in": round(meal["carb"], 1),
            "sodium_in": round(meal["sodium"], 1),
            "trips": commute["trips"],
            "distance_km": round(commute["meters"] / 1000, 2),
            # 消耗只统计通勤（走/骑过去烧掉的），不混进摄入侧的总卡路里
            "kcal_burned": round(commute["kcal"], 1),
            "commute_minutes": round(commute["minutes"], 1),
            "words_studied": words["studied"],
            "words_mastered": words["mastered"],
            "words_correct": words["correct"],
            "words_wrong": words["wrong"],
            "word_accuracy": round(words["correct"] / total_words * 100, 1) if total_words else 0.0,
            "sessions": session["sessions"],
            # 专注时间 = sessions 表里真实记录的时长（背单词 + 等餐），全部落库可追溯
            "focus_minutes": round(session["minutes"], 1),
            "focus_words_minutes": round(session["words_min"], 1),
            "focus_wait_minutes": round(session["wait_min"], 1),
            "active_days": active_days["days"],
            "streak_days": self._streak(),
        }

    def _active_dates(self) -> List[str]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT DISTINCT day FROM (
                       SELECT substr(ts, 1, 10) AS day FROM meals
                       UNION SELECT substr(ts, 1, 10) FROM commutes
                       UNION SELECT substr(ts, 1, 10) FROM sessions
                   ) ORDER BY day DESC"""
            ).fetchall()
        return [row["day"] for row in rows]

    def _streak(self) -> int:
        days = set(self._active_dates())
        if not days:
            return 0
        streak = 0
        cursor = date.today()
        # 今天没记录时，从昨天开始算，避免早上打开就断签
        if cursor.isoformat() not in days:
            cursor -= timedelta(days=1)
        while cursor.isoformat() in days:
            streak += 1
            cursor -= timedelta(days=1)
        return streak

    # ------------------------------------------------------------------ 成就
    BADGES: List[Tuple[str, str, str]] = [
        ("first_meal", "第一餐达标", "有一餐落在热量与蛋白目标区间内"),
        ("meals_10", "十餐知己", "累计 10 餐达标"),
        ("words_50", "五十词", "累计学习 50 个单词"),
        ("words_100", "百词斩", "累计学习 100 个单词"),
        ("mastered_30", "三十而熟", "30 个单词进入长期记忆（间隔 ≥ 7 天）"),
        ("burn_500", "燃烧五百", "通勤累计消耗 500 kcal"),
        ("walk_10km", "十公里", "步行/骑行累计 10 km"),
        ("streak_3", "三日不断", "连续 3 天有记录"),
        ("protein_1kg", "蛋白一公斤", "累计摄入蛋白质 1000 g"),
    ]

    def achievements(self, stats: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        s = stats or self.stats()
        conditions = {
            "first_meal": s["meals_on_target"] >= 1,
            "meals_10": s["meals_on_target"] >= 10,
            "words_50": s["words_studied"] >= 50,
            "words_100": s["words_studied"] >= 100,
            "mastered_30": s["words_mastered"] >= 30,
            "burn_500": s["kcal_burned"] >= 500,
            "walk_10km": s["distance_km"] >= 10,
            "streak_3": s["streak_days"] >= 3,
            "protein_1kg": s["protein_in"] >= 1000,
        }
        unlocked = {}
        with self._conn() as conn:
            for row in conn.execute("SELECT code, unlocked_at FROM badges").fetchall():
                unlocked[row["code"]] = row["unlocked_at"]
            for code, ok in conditions.items():
                if ok and code not in unlocked:
                    stamp = datetime.now().isoformat(timespec="seconds")
                    conn.execute("INSERT INTO badges (code, unlocked_at) VALUES (?, ?)", (code, stamp))
                    unlocked[code] = stamp

        result = []
        for code, name, desc in self.BADGES:
            result.append(
                {
                    "code": code,
                    "name": name,
                    "desc": desc,
                    "unlocked": code in unlocked,
                    "unlocked_at": unlocked.get(code),
                    "progress": s,
                }
            )
        return result


    # ---------------------------------------------------- 同步（手机 ↔ 电脑）
    def _uid_rowid(self, table: str, uid: str) -> Optional[int]:
        """同一个 uid 已经落过库就返回它的 id（手机重传时不再重复计一次）。"""
        with self._conn() as conn:
            row = conn.execute(f"SELECT id FROM {table} WHERE uid = ?", (uid,)).fetchone()
        return int(row["id"]) if row else None

    def uid_rowid(self, table: str, uid: str) -> Optional[int]:
        """公开版：这条记录是不是已经落过库（手机补传时的重传保护）。"""
        return self._uid_rowid(table, uid)

    def set_meta(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO sync_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self, key: str, default: str = "") -> str:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM sync_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def all_meta(self) -> Dict[str, str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT key, value FROM sync_meta").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def sync_snapshot(self, since: Optional[str] = None) -> Dict[str, Any]:
        """导出一份可搬到另一台设备的成长档案。

        since 传 ISO 时间则只带这之后的餐/通勤/会话；单词进度是聚合状态，
        永远全量（它只有「当前到哪了」，没有可增量追加的事件流）。
        """
        where = "WHERE ts >= ?" if since else ""
        args: Tuple[Any, ...] = (since,) if since else ()
        with self._conn() as conn:
            meals = conn.execute(
                f"SELECT * FROM meals {where} ORDER BY id", args
            ).fetchall()
            commutes = conn.execute(
                f"SELECT * FROM commutes {where} ORDER BY id", args
            ).fetchall()
            sessions = conn.execute(
                f"SELECT * FROM sessions {where} ORDER BY id", args
            ).fetchall()
            words = conn.execute("SELECT * FROM word_progress").fetchall()
            anchors = conn.execute("SELECT * FROM food_anchors").fetchall()
            badges = conn.execute("SELECT * FROM badges").fetchall()
            profile = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()

        def rows_to_list(rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
            return [dict(r) for r in rows]

        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "since": since or "",
            "profile": dict(profile) if profile else None,
            "meals": rows_to_list(meals),
            "commutes": rows_to_list(commutes),
            "sessions": rows_to_list(sessions),
            "word_progress": rows_to_list(words),
            "food_anchors": rows_to_list(anchors),
            "badges": rows_to_list(badges),
            "counts": {
                "meals": len(meals),
                "commutes": len(commutes),
                "sessions": len(sessions),
                "word_progress": len(words),
            },
        }

    def merge_snapshot(self, snap: Dict[str, Any]) -> Dict[str, Any]:
        """把另一台设备导出的档案并入本机（按 uid 去重，缺 uid 的按时间+内容指纹）。

        这是「手机带回一份 JSON → 电脑导入」那条路的落库逻辑，
        和 /api/sync/push 用的是同一套幂等规则。
        """
        applied = {"meals": 0, "commutes": 0, "sessions": 0, "word_progress": 0, "badges": 0}
        for row in snap.get("meals") or []:
            uid = str(row.get("uid") or "")
            if not uid:
                uid = f"snap-meal-{row.get('ts')}-{round(float(row.get('kcal') or 0), 1)}"
            if self._uid_rowid("meals", uid):
                continue
            try:
                items = json.loads(row.get("items_json") or "[]")
            except (TypeError, ValueError):
                items = []
            self.log_meal(
                items=items,
                kcal=float(row.get("kcal") or 0),
                protein=float(row.get("protein") or 0),
                fat=float(row.get("fat") or 0),
                carb=float(row.get("carb") or 0),
                sodium=float(row.get("sodium") or 0),
                target_kcal=row.get("target_kcal"),
                store=str(row.get("store") or ""),
                meal_type=str(row.get("meal_type") or ""),
                source=str(row.get("source") or "sync"),
                ref_id=str(row.get("ref_id") or ""),
                ts=_parse_ts(row.get("ts")),
                uid=uid,
            )
            applied["meals"] += 1

        for row in snap.get("commutes") or []:
            uid = str(row.get("uid") or "")
            if not uid:
                uid = f"snap-commute-{row.get('ts')}-{round(float(row.get('kcal') or 0), 1)}"
            if self._uid_rowid("commutes", uid):
                continue
            self.log_commute(
                store=str(row.get("store") or ""),
                mode=str(row.get("mode") or "walk"),
                distance_m=float(row.get("distance_m") or 0),
                minutes=float(row.get("minutes") or 0),
                kcal=float(row.get("kcal") or 0),
                source=str(row.get("source") or "sync"),
                ref_id=str(row.get("ref_id") or ""),
                ts=_parse_ts(row.get("ts")),
                uid=uid,
            )
            applied["commutes"] += 1

        for row in snap.get("sessions") or []:
            uid = str(row.get("uid") or "")
            if not uid:
                uid = f"snap-session-{row.get('ts')}-{row.get('kind')}-{round(float(row.get('duration_min') or 0), 2)}"
            if self._uid_rowid("sessions", uid):
                continue
            try:
                meta = json.loads(row.get("meta_json") or "{}")
            except (TypeError, ValueError):
                meta = {}
            self.log_session(
                kind=str(row.get("kind") or "words"),
                duration_min=float(row.get("duration_min") or 0),
                words=int(row.get("words") or 0),
                correct=int(row.get("correct") or 0),
                kcal_burned=float(row.get("kcal_burned") or 0),
                meta=meta,
                ts=_parse_ts(row.get("ts")),
                uid=uid,
            )
            applied["sessions"] += 1

        with self._conn() as conn:
            for row in snap.get("word_progress") or []:
                word = str(row.get("word") or "")
                if not word:
                    continue
                conn.execute(
                    """INSERT INTO word_progress (word, reps, ease, interval_d, due_date, correct, wrong, last_ts)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(word) DO UPDATE SET
                         reps=MAX(word_progress.reps, excluded.reps),
                         correct=MAX(word_progress.correct, excluded.correct),
                         wrong=MAX(word_progress.wrong, excluded.wrong),
                         due_date=MAX(word_progress.due_date, excluded.due_date),
                         ease=(word_progress.ease + excluded.ease) / 2,
                         last_ts=MAX(COALESCE(word_progress.last_ts, ''), excluded.last_ts)""",
                    (
                        word,
                        int(row.get("reps") or 0),
                        float(row.get("ease") or 2.5),
                        int(row.get("interval_d") or 1),
                        str(row.get("due_date") or TODAY()),
                        int(row.get("correct") or 0),
                        int(row.get("wrong") or 0),
                        str(row.get("last_ts") or ""),
                    ),
                )
                applied["word_progress"] += 1
            for row in snap.get("badges") or []:
                code = str(row.get("code") or "")
                if not code:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO badges (code, unlocked_at) VALUES (?, ?)",
                    (code, str(row.get("unlocked_at") or datetime.now().isoformat(timespec="seconds"))),
                )
                applied["badges"] += 1

        profile = snap.get("profile")
        if profile and profile.get("height_cm"):
            # 只在对方档案更新的情况下覆盖，避免旧手机把新数据顶掉
            incoming = str(profile.get("updated_at") or "")
            current = self.get_meta("profile_updated_at", "")
            if incoming >= current:
                self.save_profile(
                    Profile(
                        height_cm=float(profile.get("height_cm") or 0),
                        weight_kg=float(profile.get("weight_kg") or 0),
                        age=int(profile.get("age") or 0),
                        sex=str(profile.get("sex") or "male"),
                        activity=str(profile.get("activity") or "light"),
                        goal=str(profile.get("goal") or "maintain"),
                    )
                )
                self.set_meta("profile_updated_at", incoming)
        return applied

    def recent_meals(self, limit: int = 5) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM meals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def logged_ref_ids(self) -> set:
        """已经记入档案的订单号——避免同一单被重复点「记入」。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ref_id FROM meals WHERE ref_id IS NOT NULL AND ref_id <> ''"
            ).fetchall()
        return {str(row["ref_id"]) for row in rows}
