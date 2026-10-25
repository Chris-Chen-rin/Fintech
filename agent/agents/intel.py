"""A1 情報 Agent：從公開資訊觀測站重大訊息擷取文字型事實（L1）。

規則版：依主旨關鍵字挑出營運相關的公告，事實只寫「誰、何時、公告主旨」與距交易日天數。
LLM 版：讀公告全文，挑出與投資判斷有關的訊息並改寫成一句事實；
程式檢查每個數字都真的出現在公告原文、不含推論用語，不合格的條目直接丟掉。
"""
from __future__ import annotations

import re
from typing import Any

from dplan_checker import rules as R

from ..framework.context import RunContext
from ..framework.contracts import Fact
from .llm_base import LLMAgent

MATERIAL = ("營收", "盈餘", "損益", "財務報", "股利", "配息", "法說", "法人說明", "自結", "取得", "處分", "增資",
            "減資", "合併", "收購", "併購", "訂單", "擴產", "投資", "庫藏股", "背書", "資金貸與", "澄清", "停工",
            "火災", "裁罰", "訴訟", "董事會決議", "買回")
ROUTINE = ("代表人改派", "更名", "面額", "股東常會", "股東臨時會", "公告期間", "董事長異動", "發言人", "會計主管",
           "稽核主管", "獨立董事", "薪資報酬委員", "審計委員", "董事異動", "監察人", "新任", "辭任")
MAX_FACTS = 20
PER_STOCK = 2


def _clean(text: str) -> bool:
    return not any(w in text for w in R.INFERENCE_WORDS)


def candidates(ctx: RunContext, limit: int = 60) -> list[Any]:
    """與營運有關、且主旨沒有推論用語的公告；新的在前，每檔最多 PER_STOCK 則。"""
    out, per = [], {}
    for a in getattr(ctx, "announcements", None) or []:
        if any(w in a.subject for w in ROUTINE) or not any(w in a.subject for w in MATERIAL) or not _clean(a.subject):
            continue
        if per.get(a.code, 0) >= PER_STOCK:
            continue
        per[a.code] = per.get(a.code, 0) + 1
        out.append(a)
    return out[:limit]


def _days_before(ctx: RunContext, a: Any) -> float:
    return float((ctx.window.trade_date - a.at.date()).days)


def _fact(ctx: RunContext, key: str, a: Any, body: str, values: dict[str, float]) -> Fact:
    head = f"{ctx.name(a.code)} {a.at:%m/%d %H:%M} 重大訊息："
    return Fact(key, [a.source_key], (head + body)[:500], values | {"days_before_trade": _days_before(ctx, a)},
                "intel", a.code)


def _keys(ctx: RunContext, n: int) -> list[str]:
    base = int(ctx.facts.next_key()[1:])
    return [f"F{base + i}" for i in range(n)]


class RuleIntelAgent:
    name = "intel"
    variant = "rule"

    def run(self, ctx: RunContext, inputs: dict[str, Any], feedback: list[str] | None = None) -> list[Fact]:
        picked = candidates(ctx)[:MAX_FACTS]
        return [_fact(ctx, k, a, a.subject[:200], {}) for k, a in zip(_keys(ctx, len(picked)), picked)]


_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _numbers_in(text: str) -> set[float]:
    return {float(x) for x in _NUM.findall(text.replace(",", "").replace("，", ""))}


class LLMIntelAgent(LLMAgent):
    name = "intel"

    def build_prompt(self, ctx: RunContext, inputs: dict[str, Any], feedback: list[str] | None) -> tuple[str, str]:
        self._cands = candidates(ctx)
        system = (
            "你是台股 ETF 投資團隊的情報分析師。輸入是 150 檔標的最近幾天在公開資訊觀測站發布的重大訊息。"
            f"請挑出最多 {MAX_FACTS} 則對投資判斷有用的訊息（營收、獲利、股利、併購、擴產、重大訂單、資產處分、裁罰等），"
            "每則寫成一句事實（30～150 字），只寫公告中明確記載的內容與數字，不要寫評價、預測或建議，"
            f"也不要使用這些字詞：{'、'.join(R.INFERENCE_WORDS)}。"
            "values 放 1～4 個公告原文中出現的數字（鍵名用英文 snake_case，例如 eps、revenue_yoy_pct、amount_million_twd），"
            "數字照抄原文但去掉千分位逗號（1,022,129,557 寫成 1022129557），不可換算；找不到合適數字時 values 給 {}。"
            "statement 內不要使用英文雙引號。不需要長時間思考，直接輸出 JSON。"
            '輸出 JSON：{"facts": [{"idx": 編號, "statement": "…", "values": {"鍵": 數字}}]}；沒有值得記錄的就輸出 {"facts": []}。'
        )
        lines = [f"[{i}] {a.code} {a.name} {a.at:%Y-%m-%d %H:%M} 主旨：{a.subject}\n說明：{a.detail[:500]}"
                 for i, a in enumerate(self._cands)]
        user = f"交易日 {ctx.window.trade_date}。重大訊息共 {len(lines)} 則：\n\n" + "\n\n".join(lines or ["（無）"])
        return system, user

    def parse(self, ctx: RunContext, inputs: dict[str, Any], data: Any) -> list[Fact]:
        items = data.get("facts") if isinstance(data, dict) else None
        if not isinstance(items, list):
            from ..llm.base import LLMError
            raise LLMError("情報輸出缺少 facts 陣列")
        kept: list[tuple[Any, str, dict[str, float]]] = []
        used: set[int] = set()
        for it in items:
            if not isinstance(it, dict):
                continue
            idx, text = it.get("idx"), it.get("statement")
            if not isinstance(idx, int) or not 0 <= idx < len(self._cands) or idx in used:
                continue
            if not isinstance(text, str) or not 10 <= len(text.strip()) <= 300 or not _clean(text):
                continue
            a = self._cands[idx]
            allowed = _numbers_in(a.text) | {float(x) for x in (a.at.year, a.at.year - 1911, a.at.month, a.at.day)}
            if not _numbers_in(text) <= allowed:  # 事實句中的數字也必須出自公告原文
                continue
            values = {str(k)[:40]: float(v) for k, v in (it.get("values") or {}).items()
                      if isinstance(v, (int, float)) and not isinstance(v, bool) and float(v) in allowed}
            used.add(idx)
            kept.append((a, text.strip(), dict(list(values.items())[:4])))
        kept = kept[:MAX_FACTS]
        return [_fact(ctx, k, a, body, vals) for k, (a, body, vals) in zip(_keys(ctx, len(kept)), kept)]
