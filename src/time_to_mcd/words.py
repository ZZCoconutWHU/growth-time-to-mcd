"""单词模块：把「等餐的 7 分钟」变成「背 40 个词」的时间。

组成：
  - 词库：`data/wordbank.json`（内置 CET-4 节选，可自行扩充）
  - 选题：到期的词优先（间隔重复），其次生词，最后随机复习
  - 题型：英译中 / 中译英 四选一，干扰项同级别随机
  - 记录：每次作答写回 SQLite，推进 SM-2 间隔
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .store import RecordStore

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DEFAULT_BANK = DATA_DIR / "wordbank.json"

# 一题平均 10 秒左右，含思考与重看
SECONDS_PER_WORD = 10
WORDS_PER_MINUTE = 60 // SECONDS_PER_WORD


@dataclass
class Word:
    en: str
    zh: str
    pos: str = ""
    level: str = "CET4"


@dataclass
class Question:
    word: Word
    direction: str  # en2zh / zh2en
    prompt: str
    options: List[str]
    answer: int

    @property
    def answer_text(self) -> str:
        return self.options[self.answer]


@dataclass
class SessionResult:
    total: int = 0
    correct: int = 0
    new_mastered: int = 0
    duration_min: float = 0
    mistakes: List[Word] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total * 100 if self.total else 0.0


def load_wordbank(path: Optional[Path] = None) -> List[Word]:
    file = Path(path) if path else DEFAULT_BANK
    if not file.is_file():
        return []
    payload = json.loads(file.read_text(encoding="utf-8"))
    words = []
    for item in payload.get("words", []):
        if item.get("en") and item.get("zh"):
            words.append(Word(en=item["en"], zh=item["zh"], pos=item.get("pos", ""), level=item.get("level", "CET4")))
    return words


def plan_count(minutes: float, maximum: int = 60) -> int:
    """按可用分钟数估算能刷多少题，至少 4 题，避免刚热身就结束。"""
    return max(4, min(maximum, int(round(minutes * WORDS_PER_MINUTE))))


def select_words(
    bank: Sequence[Word],
    count: int,
    due: Sequence[str] = (),
    progress: Optional[Dict[str, Dict[str, Any]]] = None,
    rng: Optional[random.Random] = None,
) -> List[Word]:
    """到期的优先复习，其次没见过的，最后随便抽几个巩固。"""
    rng = rng or random.Random()
    progress = progress or {}
    by_en = {w.en: w for w in bank}

    picked: List[Word] = []
    seen: set = set()

    for word in due:
        if word in by_en and word not in seen:
            picked.append(by_en[word])
            seen.add(word)
        if len(picked) >= count:
            return picked

    fresh = [w for w in bank if w.en not in progress]
    rng.shuffle(fresh)
    for word in fresh:
        if len(picked) >= count:
            return picked
        picked.append(word)
        seen.add(word.en)

    rest = [w for w in bank if w.en not in seen]
    rng.shuffle(rest)
    for word in rest:
        if len(picked) >= count:
            break
        picked.append(word)

    return picked[:count]


def build_questions(words: Sequence[Word], bank: Sequence[Word], rng: Optional[random.Random] = None) -> List[Question]:
    rng = rng or random.Random()
    questions: List[Question] = []
    for index, word in enumerate(words):
        direction = "zh2en" if index % 3 == 2 else "en2zh"
        pool = [w for w in bank if w.zh != word.zh and w.en != word.en]
        same_level = [w for w in pool if w.level == word.level] or pool
        rng.shuffle(same_level)
        distractors = same_level[:3]
        while len(distractors) < 3 and pool:  # 词库过小时兜底
            distractors.append(rng.choice(pool))

        if direction == "en2zh":
            prompt = f"{word.en}  的中文意思是？"
            correct = word.zh
            options = [correct] + [w.zh for w in distractors[:3]]
        else:
            prompt = f"「{word.zh}」对应的英文是？"
            correct = word.en
            options = [correct] + [w.en for w in distractors[:3]]

        rng.shuffle(options)
        questions.append(
            Question(
                word=word,
                direction=direction,
                prompt=prompt,
                options=options,
                answer=options.index(correct),
            )
        )
    return questions


def parse_choice(raw: str, option_count: int = 4) -> Optional[int]:
    text = (raw or "").strip().upper()
    if not text:
        return None
    if text[0] in "ABCD" and len(text) == 1:
        index = ord(text) - ord("A")
        return index if index < option_count else None
    matched = re.search(r"\d+", text)
    if matched:
        index = int(matched.group()) - 1
        return index if 0 <= index < option_count else None
    return None


def play_session(
    store: RecordStore,
    minutes: float,
    bank: Optional[Sequence[Word]] = None,
    ask: Callable[[str], str] = input,
    tell: Callable[[str], None] = print,
    answers: Optional[Sequence[str]] = None,
    seed: Optional[int] = None,
) -> SessionResult:
    """跑一局单词游戏，逐题写回进度。

    answers 用于无人值守演示：按顺序给答案（如 ["1","3","2"...]），
    正常交互时传 None，由 ask() 读输入。
    """
    bank = list(bank or load_wordbank())
    if not bank:
        raise RuntimeError("词库为空：请检查 data/wordbank.json")

    rng = random.Random(seed)
    count = plan_count(minutes)
    progress = store.word_progress()
    words = select_words(bank, count, due=store.due_words(count), progress=progress, rng=rng)
    questions = build_questions(words, bank, rng=rng)

    result = SessionResult(total=len(questions), duration_min=minutes)
    tell(f"开始一局：{len(questions)} 题，预计 {minutes:g} 分钟。答对会自动拉长复习间隔。\n")

    for index, question in enumerate(questions):
        tell(f"[{index + 1}/{len(questions)}] {question.prompt}")
        for label, option in zip("ABCD", question.options):
            tell(f"    {label}. {option}")

        raw = answers[index] if answers and index < len(answers) else ask("你的答案（A/B/C/D）: ")
        choice = parse_choice(raw, len(question.options))
        if choice is None:
            tell("    跳过（未识别的选项）\n")
            result.mistakes.append(question.word)
            continue

        ok = choice == question.answer
        outcome = store.record_word(question.word.en, ok)
        if ok:
            result.correct += 1
            if outcome.mastered and outcome.reps == 3:
                result.new_mastered += 1
            tell(f"    正确。下次复习：{outcome.due_date}（间隔 {outcome.interval_d} 天）\n")
        else:
            result.mistakes.append(question.word)
            tell(f"    错了，正确答案是 {chr(ord('A') + question.answer)}. {question.answer_text}\n")

    store.log_session(
        kind="words",
        duration_min=minutes,
        words=result.total,
        correct=result.correct,
        meta={"new_mastered": result.new_mastered, "mistakes": [w.en for w in result.mistakes][:20]},
    )
    return result
