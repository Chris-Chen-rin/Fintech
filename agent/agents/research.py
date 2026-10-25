"""A3 研究 Agent：個股事實 → 個股推論（看法、分數、反方訊號）。"""
from __future__ import annotations

import statistics
from typing import Any

from ..framework.context import RunContext
from ..framework.contracts import Thesis

MIN_AVG_VALUE = 2e8  # 近 20 日平均成交值低於 2 億元視為流動性不足
MOMENTUM_WEIGHTS = {"ret_60d": 0.4, "ret_20d": 0.4, "ret_5d": 0.2}
QUALITY_WEIGHTS = {"roe": 0.35, "eps_growth": 0.25, "rev_cum_yoy": 0.25, "cash_conversion": 0.15}
QUALITY_CLIP = {"roe": (-20, 60), "eps_growth": (-100, 200), "rev_cum_yoy": (-50, 100), "cash_conversion": (-1, 3)}
QUALITY_SHARE = 0.5  # 綜合分數 = 0.5×品質 + 0.5×動能（沒有財報資料時全看動能）
LOSS_PENALTY = 1.0


def zscores(values: dict[str, float]) -> dict[str, float]:
    if len(values) < 2:
        return {k: 0.0 for k in values}
    mu = statistics.fmean(values.values())
    sd = statistics.pstdev(values.values()) or 1.0
    return {k: max(-3.0, min(3.0, (v - mu) / sd)) for k, v in values.items()}


def weighted_z(candidates: list[str], getter, weights: dict[str, float],
               clip: dict[str, tuple[float, float]] | None = None) -> dict[str, float]:
    """各指標 z 分數加權；缺值的指標以 0（平均）計。"""
    out = {c: 0.0 for c in candidates}
    for name, w in weights.items():
        raw = {c: v for c in candidates if (v := getter(c, name)) is not None}
        if clip and name in clip:
            lo, hi = clip[name]
            raw = {c: max(lo, min(hi, v)) for c, v in raw.items()}
        for c, z in zscores(raw).items():
            out[c] += w * z
    return out


class RuleResearchAgent:
    """優質動能規則：品質（ROE、EPS 年增、營收年增、現金流量）＋動能（60／20／5 日報酬），近四季虧損扣分。"""
    name = "research"
    variant = "rule"

    def run(self, ctx: RunContext, inputs: dict[str, Any], feedback: list[str] | None = None) -> list[Thesis]:
        metrics = ctx.metrics
        candidates = [c for c in inputs["candidates"] if c in metrics and metrics[c].fact]
        weak = set(inputs.get("weak") or [])
        theme = ctx.theme.members if ctx.theme else set()
        theme_name = ctx.settings.theme.name or "本 ETF 主題"
        taiex_fact = inputs.get("taiex_trend_fact")
        taiex_20d = ctx.market_metrics.get("taiex_ret_20d")

        fund = {c: metrics[c].fund for c in candidates if metrics[c].fund is not None and metrics[c].fund_fact}
        momentum = weighted_z(candidates, lambda c, n: getattr(metrics[c], n), MOMENTUM_WEIGHTS)
        quality = weighted_z(list(fund), lambda c, n: getattr(fund[c], n), QUALITY_WEIGHTS, QUALITY_CLIP)
        q_share = QUALITY_SHARE if fund else 0.0
        scores: dict[str, float] = {}
        for c in candidates:
            s = (1 - q_share) * momentum[c] + q_share * quality.get(c, 0.0)
            if (metrics[c].avg_value or 0) < MIN_AVG_VALUE:
                s -= 1.0
            if c in fund and fund[c].profitable_ttm is False:
                s -= LOSS_PENALTY
            scores[c] = round(s, 3)
        ranking = {c: i + 1 for i, c in enumerate(sorted(scores, key=lambda c: -scores[c]))}

        out: list[Thesis] = []
        for c in candidates:
            m, s, f = metrics[c], scores[c], fund.get(c)
            premises = [m.fact]
            parts = [f"近 {n} 個交易日 {v:+.2f}%" for n, v in ((60, m.ret_60d), (20, m.ret_20d), (5, m.ret_5d)) if v is not None]
            logic = f"{c} {m.name}（{{{m.fact}}}）" + ("、".join(parts) or f"收盤 {m.close:g} 元")
            if taiex_fact and taiex_20d is not None and m.ret_20d is not None:
                premises.append(taiex_fact)
                logic += f"，相對加權指數（{{{taiex_fact}}}）超額 {m.ret_20d - taiex_20d:+.2f} 個百分點"
            logic += f"，動能分數 {momentum[c]:+.2f}"
            if f is not None:
                premises.append(m.fund_fact)
                q = []
                if f.eps_ttm is not None:
                    q.append(f"近四季 EPS {f.eps_ttm:g} 元")
                if f.roe is not None:
                    q.append(f"以股價淨值比÷本益比推算 ROE 約 {f.roe:.1f}%")
                if f.eps_growth is not None:
                    q.append(f"{f.season.label} EPS 年增 {f.eps_growth:+.1f}%")
                if f.rev_cum_yoy is not None:
                    q.append(f"累計營收年增 {f.rev_cum_yoy:+.1f}%")
                if f.cash_conversion is not None:
                    q.append(f"營業現金流為淨利的 {f.cash_conversion:.2f} 倍")
                logic += f"；基本面（{{{m.fund_fact}}}）" + ("、".join(q) or "資料不足") + f"，品質分數 {quality[c]:+.2f}"
                if f.profitable_ttm is False:
                    logic += "，近四季虧損，不符合優質條件，扣分"
            logic += f"；綜合分數 {s:+.2f}，在 {len(candidates)} 檔候選中排第 {ranking[c]} 名"
            if (m.avg_value or 0) < MIN_AVG_VALUE:
                logic += "，近 20 日平均成交值不足 2 億元，流動性扣分"
            if c in theme:
                reason = ctx.theme.reasons.get(c)
                logic += f"；屬於「{theme_name}」主題標的" + (f"（{reason}）" if reason else "")
            elif theme:
                miss = ctx.theme.misses.get(c)
                logic += (f"；不在「{theme_name}」核心選股池" + (f"（{miss}）" if miss else "")
                          + "，僅能作為分散用的衛星持股，檔數受核心池比重限制")
            news = [f.key for f in ctx.facts.of(c) if f.topic == "intel"][:2]
            if news:
                premises += news
                logic += "；近期重大訊息（" + "、".join(f"{{{k}}}" for k in news) + "）列入追蹤，未納入分數"

            trend = m.ret_60d if m.ret_60d is not None else m.ret_20d
            if c in weak or (f is not None and f.profitable_ttm is False and s > -0.5):
                view = "neutral"
            elif s >= 0.25 and (trend or 0) > 0:
                view = "positive"
            elif s <= -0.5:
                view = "negative"
            else:
                view = "neutral"
            label = {"positive": "正向，列為優先持有", "neutral": "中性", "negative": "負向，不宜新增"}[view]
            logic += f"。依優質動能規則評為{label}。"

            counters = []
            if m.ret_5d is not None and m.ret_5d < 0 < (m.ret_20d or 0):
                counters.append(f"近 5 個交易日 {m.ret_5d:+.2f}%，短線動能轉弱")
            if m.ret_20d is not None and m.ret_20d > 30:
                counters.append(f"近 20 個交易日已上漲 {m.ret_20d:.1f}%，追高風險偏高")
            if m.ret_20d is not None and m.ret_20d < 0 < (m.ret_5d or 0):
                counters.append(f"近 5 個交易日反彈 {m.ret_5d:+.2f}%，可能止跌")
            if f is not None:
                if f.pe is not None and f.pe > 60:
                    counters.append(f"本益比 {f.pe:g} 倍，評價偏高")
                if f.eps_growth is not None and f.eps_growth < -20:
                    counters.append(f"EPS 年減 {abs(f.eps_growth):.1f}%，獲利動能轉弱")
                if f.cash_conversion is not None and f.cash_conversion < 0.5:
                    counters.append("營業現金流不到淨利的一半，獲利品質需留意")
            if c in weak:
                counters.append("反方審查認為論據不足，看法下修為中性")
            out.append(Thesis(
                key=f"T:{c}", tickers=[c], premises=premises, logic=logic[:1000],
                conclusion=f"{c} {m.name} 優質動能{label}（分數 {s:+.2f}，第 {ranking[c]} 名）"[:300],
                counter_evidence="；".join(counters) if counters else None,
                view=view, score=s, confidence=round(min(0.85, 0.5 + abs(s) / 6), 2), author="research/rule",
            ))
        return out
