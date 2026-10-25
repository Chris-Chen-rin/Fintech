"""LLM 呼叫額度：每次執行上限與每日上限（OpenRouter 免費模型每天 50 次，UTC 00:00 重置）。

每日計數存在快取資料夾，正式執行與演練共用；超過上限時 LLM 版直接失敗，由備援鏈降級為規則版。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .base import LLMError

FILE = "llm_calls.json"


class BudgetExceeded(LLMError):
    """額度用完：重試也沒用，直接換下一個版本。"""
    final = True


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def daily_count(cache: Path) -> int:
    path = cache / FILE
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get(_today(), 0))
    except (OSError, ValueError, AttributeError):
        return 0


def reserve(ctx) -> None:
    """呼叫前先佔一次額度；超過上限就丟 LLMError。"""
    llm = ctx.settings.llm
    if ctx.llm_calls >= llm.max_calls_per_run:
        raise BudgetExceeded(f"本次執行的 LLM 呼叫已達上限 {llm.max_calls_per_run} 次，改用備援")
    cache = ctx.settings.paths.cache
    used = daily_count(cache)
    if used >= llm.daily_call_limit:
        raise BudgetExceeded(f"今日（UTC）LLM 呼叫已達上限 {llm.daily_call_limit} 次，改用備援")
    ctx.llm_calls += 1
    path = cache / FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    data = {k: v for k, v in data.items() if k >= (dt.date.fromisoformat(_today()) - dt.timedelta(days=14)).isoformat()}
    data[_today()] = used + 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
