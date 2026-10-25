"""LLM 呼叫額度：每次執行上限、每日總上限、個別模型的每日上限（稀缺模型用）。

日期以太平洋時間計算（Gemini 免費額度在太平洋時間午夜重置，約台北 15:00～16:00）。
計數存在快取資料夾，正式執行與演練共用；超過上限時 LLM 版直接失敗，由備援鏈降級，不中斷流程。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from .base import LLMError

FILE = "llm_calls.json"
QUOTA_TZ = dt.timezone(dt.timedelta(hours=-8))  # 太平洋標準時間；夏令時間期間換日晚 1 小時，只會多算不會少算
TOTAL = "_total"


class BudgetExceeded(LLMError):
    """額度用完：重試也沒用，直接換下一個版本。"""
    final = True


def _today() -> str:
    return dt.datetime.now(QUOTA_TZ).date().isoformat()


def _load(cache: Path) -> dict[str, Any]:
    try:
        data = json.loads((cache / FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _day(cache: Path) -> dict[str, int]:
    day = _load(cache).get(_today(), {})
    return day if isinstance(day, dict) else {TOTAL: int(day)}


def _key(agent: str, spec: Any) -> str:
    return f"{agent}|{spec.provider}:{spec.model}"


def daily_count(cache: Path) -> int:
    return int(_day(cache).get(TOTAL, 0))


def model_count(cache: Path, agent: str, spec: Any) -> int:
    return int(_day(cache).get(_key(agent, spec), 0))


def reserve(ctx, agent: str, spec: Any) -> None:
    """呼叫前先佔一次額度；任何一個上限用完就丟 BudgetExceeded。"""
    llm = ctx.settings.llm
    cache = ctx.settings.paths.cache
    if ctx.llm_calls >= llm.max_calls_per_run:
        raise BudgetExceeded(f"本次執行的 LLM 呼叫已達上限 {llm.max_calls_per_run} 次，改用備援")
    day = _day(cache)
    if day.get(TOTAL, 0) >= llm.daily_call_limit:
        raise BudgetExceeded(f"今日 LLM 呼叫已達總上限 {llm.daily_call_limit} 次，改用備援")
    key = _key(agent, spec)
    if spec.daily_limit is not None and day.get(key, 0) >= spec.daily_limit:
        raise BudgetExceeded(f"{spec.model} 今日給 {agent} 的 {spec.daily_limit} 次已用完，改用備援")
    ctx.llm_calls += 1
    day[TOTAL] = day.get(TOTAL, 0) + 1
    day[key] = day.get(key, 0) + 1
    data = _load(cache)
    cutoff = (dt.date.fromisoformat(_today()) - dt.timedelta(days=14)).isoformat()
    data = {k: v for k, v in data.items() if k >= cutoff}
    data[_today()] = day
    cache.mkdir(parents=True, exist_ok=True)
    (cache / FILE).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
