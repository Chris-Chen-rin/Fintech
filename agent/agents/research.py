"""A3 研究 Agent：個股事實 → 個股推論（看法、分數、反方訊號）。"""
from __future__ import annotations

import statistics
from typing import Any

from ..framework.context import RunContext
from ..framework.contracts import Thesis

MIN_AVG_VALUE = 2e8  # 近 20 日平均成交值低於 2 億元視為流動性不足


def zscores(values: dict[str, float]) -> dict[str, float]:
    if len(values) < 2:
        return {k: 0.0 for k in values}
    mu = statistics.fmean(values.values())
    sd = statistics.pstdev(values.values()) or 1.0
    return {k: max(-3.0, min(3.0, (v - mu) / sd)) for k, v in values.items()}


class RuleResearchAgent:
    """動能規則：分數 = 0.6×z(20 日報酬) + 0.4×z(5 日報酬)，再扣流動性不足。"""
    name = "research"
    variant = "rule"

    def run(self, ctx: RunContext, inputs: dict[str, Any], feedback: list[str] | None = None) -> list[Thesis]:
        metrics = ctx.metrics
        candidates = [c for c in inputs["candidates"] if c in metrics and metrics[c].fact]
        weak = set(inputs.get("weak") or [])
        theme = set(ctx.settings.theme.tickers)
        theme_name = ctx.settings.theme.name
        taiex_fact = inputs.get("taiex_trend_fact")
        taiex_20d = ctx.market_metrics.get("taiex_ret_20d")

        r20 = {c: metrics[c].ret_20d for c in candidates if metrics[c].ret_20d is not None}
        r5 = {c: metrics[c].ret_5d for c in candidates if metrics[c].ret_5d is not None}
        z20, z5 = zscores(r20), zscores(r5)
        scores: dict[str, float] = {}
        for c in candidates:
            s = 0.6 * z20.get(c, 0.0) + 0.4 * z5.get(c, 0.0)
            if (metrics[c].avg_value or 0) < MIN_AVG_VALUE:
                s -= 1.0
            scores[c] = round(s, 3)
        ranking = {c: i + 1 for i, c in enumerate(sorted(scores, key=lambda c: -scores[c]))}

        out: list[Thesis] = []
        for c in candidates:
            m, s = metrics[c], scores[c]
            premises = [m.fact]
            parts = []
            if m.ret_20d is not None:
                parts.append(f"近 20 個交易日 {m.ret_20d:+.2f}%")
            if m.ret_5d is not None:
                parts.append(f"近 5 個交易日 {m.ret_5d:+.2f}%")
            logic = f"{c} {m.name}（{{{m.fact}}}）" + ("、".join(parts) or f"收盤 {m.close:g} 元")
            if taiex_fact and taiex_20d is not None and m.ret_20d is not None:
                premises.append(taiex_fact)
                logic += f"，相對加權指數（{{{taiex_fact}}}）超額 {m.ret_20d - taiex_20d:+.2f} 個百分點"
            logic += f"；動能分數 {s:+.2f}，在 {len(candidates)} 檔候選中排第 {ranking[c]} 名"
            if (m.avg_value or 0) < MIN_AVG_VALUE:
                logic += "，近 20 日平均成交值不足 2 億元，流動性扣分"
            if c in theme and theme_name:
                logic += f"；屬於本 ETF 主題「{theme_name}」標的"

            if c in weak:
                view = "neutral"
            elif s >= 0.25 and (m.ret_20d or 0) > 0:
                view = "positive"
            elif s <= -0.5:
                view = "negative"
            else:
                view = "neutral"
            label = {"positive": "正向，列為優先持有", "neutral": "中性", "negative": "負向，不宜新增"}[view]
            logic += f"。依動能規則評為{label}。"

            counters = []
            if m.ret_5d is not None and m.ret_5d < 0 < (m.ret_20d or 0):
                counters.append(f"近 5 個交易日 {m.ret_5d:+.2f}%，短線動能轉弱")
            if m.ret_20d is not None and m.ret_20d > 30:
                counters.append(f"近 20 個交易日已上漲 {m.ret_20d:.1f}%，追高風險偏高")
            if m.ret_20d is not None and m.ret_20d < 0 < (m.ret_5d or 0):
                counters.append(f"近 5 個交易日反彈 {m.ret_5d:+.2f}%，可能止跌")
            if c in weak:
                counters.append("反方審查認為論據不足，看法下修為中性")
            out.append(Thesis(
                key=f"T:{c}", tickers=[c], premises=premises, logic=logic[:1000],
                conclusion=f"{c} {m.name} 動能{label}（分數 {s:+.2f}，第 {ranking[c]} 名）"[:300],
                counter_evidence="；".join(counters) if counters else None,
                view=view, score=s, confidence=round(min(0.85, 0.5 + abs(s) / 6), 2), author="research/rule",
            ))
        return out
