"""A4 反方審查 Agent：挑戰正向推論，論據不足的退回研究 Agent。"""
from __future__ import annotations

from typing import Any

from ..framework.context import RunContext
from ..framework.contracts import Review, Thesis
from ..tools.signals import conflicts, unaddressed


def conflict_notes(ctx: RunContext, t: Thesis) -> list[str]:
    """正向推論沒有解釋的矛盾訊號（程式偵測）。"""
    if t.view != "positive" or len(t.tickers) != 1 or t.tickers[0] not in ctx.metrics:
        return []
    return [f"未解釋：{c.text}" for c in unaddressed(t, conflicts(ctx.metrics[t.tickers[0]]))]


class RuleReviewerAgent:
    """正向推論若前一交易日重挫、近 5 日大跌，或有沒解釋的矛盾訊號（營收月減、漲多回落、評價過高），視為論據不足。"""
    name = "reviewer"
    variant = "rule"

    def run(self, ctx: RunContext, inputs: dict[str, Any], feedback: list[str] | None = None) -> list[Review]:
        out: list[Review] = []
        for t in inputs["theses"]:
            t: Thesis
            if t.view != "positive" or not t.tickers:
                out.append(Review(t.key, "ok"))
                continue
            m = ctx.metrics[t.tickers[0]]
            notes = []
            if m.chg_pct is not None and m.chg_pct <= -4:
                notes.append(f"前一交易日重挫 {m.chg_pct:+.2f}%")
            if m.ret_5d is not None and m.ret_5d <= -5:
                notes.append(f"近 5 個交易日下跌 {m.ret_5d:+.2f}%")
            notes += conflict_notes(ctx, t)
            out.append(Review(t.key, "weak" if notes else "ok", "、".join(notes)[:120] or None))
        return out
