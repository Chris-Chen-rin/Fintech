"""矛盾訊號：同一檔股票的數據互相衝突的地方（例如營收年增很高、但月減且股價自高點大跌）。

由程式計算，給反方審查與研究 Agent 用：正向推論必須解釋這些矛盾，否則退回重寫。
門檻刻意寬鬆，只挑出明顯需要說明的情況。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..framework.contracts import Thesis
from .quant import StockMetrics


@dataclass(frozen=True)
class Conflict:
    code: str  # 類型
    text: str  # 給 Agent 看的描述（只寫數字，不下結論）
    keywords: tuple[str, ...]  # 推論中出現任一字詞，視為有回應


REVENUE_WORDS = ("較上月", "月減", "月增", "上月", "單月", "月營收")
PULLBACK_WORDS = ("高點", "獲利了結", "回落", "漲多", "拉回", "回檔", "修正")
VALUATION_WORDS = ("本益比", "評價", "估值")


def conflicts(m: StockMetrics) -> list[Conflict]:
    out: list[Conflict] = []
    f = m.fund
    if f is not None and f.rev_mom is not None and f.rev_mom <= -20 and (f.rev_yoy or 0) > 0:
        out.append(Conflict("revenue_mom_drop", f"{int(f.rev_month[-2:])} 月營收年增 {f.rev_yoy:+.2f}%，但較上月 {f.rev_mom:+.2f}%",
                            REVENUE_WORDS))
    elif f is not None and f.rev_mom is not None and f.rev_prev_mom is not None and f.rev_mom < 0 and f.rev_prev_mom < 0:
        out.append(Conflict("revenue_mom_streak", f"營收連續兩個月較上月減少（{f.rev_prev_mom:+.2f}%、{f.rev_mom:+.2f}%）",
                            REVENUE_WORDS))
    if m.from_high is not None and m.from_high <= -20 and (m.low_to_high or 0) >= 40:
        out.append(Conflict("pullback_after_rally",
                            f"前波低點到高點上漲 {m.low_to_high:+.2f}%，目前距最高收盤 {m.from_high:+.2f}%", PULLBACK_WORDS))
    elif m.ret_20d is not None and m.ret_20d <= -15:
        out.append(Conflict("sharp_drop", f"近 20 個交易日 {m.ret_20d:+.2f}%", PULLBACK_WORDS + ("下跌", "跌幅")))
    if f is not None and f.pe is not None and f.pe >= 60:
        out.append(Conflict("high_valuation", f"本益比 {f.pe:g} 倍", VALUATION_WORDS))
    return out


def unaddressed(thesis: Thesis, items: list[Conflict]) -> list[Conflict]:
    """推論（logic 與反方訊號）沒有提到的矛盾。"""
    text = thesis.logic + (thesis.counter_evidence or "")
    return [c for c in items if not any(w in text for w in c.keywords)]
