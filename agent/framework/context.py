"""每次執行共用的狀態：設定、資料包、行情、來源、事實、紀錄。"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..datapack import DataPack
from ..settings import Settings
from ..sources import SourceRegistry
from ..tools.market_data import MarketHistory
from ..trading_calendar import SubmissionWindow
from .contracts import Fact


class FactBook:
    def __init__(self) -> None:
        self._facts: dict[str, Fact] = {}
        self.by_scope: dict[str, list[str]] = {}

    def add(self, sources: list[str], statement: str, values: dict[str, float], topic: str, scope: str) -> str:
        key = f"F{len(self._facts) + 1}"
        self._facts[key] = Fact(key, list(sources), statement, {k: float(v) for k, v in values.items()}, topic, scope)
        self.by_scope.setdefault(scope, []).append(key)
        return key

    def extend(self, facts: list[Fact]) -> None:
        for f in facts:
            if f.key in self._facts:
                raise ValueError(f"事實代號 {f.key} 重複")
            self._facts[f.key] = f
            self.by_scope.setdefault(f.scope, []).append(f.key)

    def next_key(self) -> str:
        return f"F{len(self._facts) + 1}"

    def get(self, key: str) -> Fact:
        return self._facts[key]

    def keys(self) -> set[str]:
        return set(self._facts)

    def of(self, scope: str) -> list[Fact]:
        return [self._facts[k] for k in self.by_scope.get(scope, [])]

    def all(self) -> list[Fact]:
        return list(self._facts.values())


@dataclass
class RunContext:
    settings: Settings
    window: SubmissionWindow
    datapack: DataPack
    universe: dict[str, str]
    markets: dict[str, str]
    history: MarketHistory
    prices: dict[str, float]
    registry: SourceRegistry
    facts: FactBook
    run_dir: Path
    started_at: dt.datetime
    clock_offset: dt.timedelta = dt.timedelta(0)
    metrics: dict[str, Any] = field(default_factory=dict)
    theme: Any = None  # ThemeMembership
    fundamentals: dict[str, Any] = field(default_factory=dict)  # 代號 → Fundamentals
    announcements: list[Any] = field(default_factory=list)  # 重大訊息（新的在前）
    market_metrics: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "tool_use_count": 0})
    log_lines: list[str] = field(default_factory=list)
    llm_calls: int = 0  # 本次執行已呼叫 LLM 的次數（含失敗）

    @property
    def book(self):
        return self.datapack.book

    @property
    def held(self) -> set[str]:
        return self.datapack.book.held_codes

    def name(self, code: str) -> str:
        return f"{code} {self.universe.get(code, '')}".strip()

    def now(self) -> dt.datetime:
        from ..trading_calendar import now_tpe
        return now_tpe() + self.clock_offset

    def log(self, message: str) -> None:
        self.log_lines.append(message)
        print(message, flush=True)

    def save(self, relative: str, payload: Any) -> Path:
        path = self.run_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2), encoding="utf-8")
        return path


def to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (dt.date, dt.datetime)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    return obj
