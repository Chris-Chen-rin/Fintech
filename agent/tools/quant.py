"""M5 量化工具：行情 → 數字型事實（L1）與給 Agent 用的內部指標。

事實裡的每個數字都能從被引用的來源快照直接核對（收盤價、漲跌、兩個日期收盤價的比值）。
成交值、波動度只作為內部篩選指標，不寫進事實。
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any

from ..framework.context import RunContext


@dataclass
class StockMetrics:
    code: str
    name: str
    market: str
    close: float
    chg_pct: float | None
    ret_5d: float | None
    ret_20d: float | None
    avg_value: float | None  # 近 20 日平均成交值（元）
    volatility: float | None  # 近 20 日日報酬標準差
    ret_60d: float | None = None
    fact: str | None = None
    fund: Any = None  # Fundamentals
    fund_fact: str | None = None
    rets: dict[int, float | None] = field(default_factory=dict)  # 交易日數 → 報酬（%），含策略設定的動能天數
    high: tuple[Any, float] | None = None  # 近 60 個交易日最高收盤（日期, 價）
    low: tuple[Any, float] | None = None
    from_high: float | None = None  # 距最高收盤 %（≤ 0）
    low_to_high: float | None = None  # 低點在高點之前時，低點到高點的漲幅 %（看前一波漲多少）

    def ret(self, n: int) -> float | None:
        return self.rets[n] if n in self.rets else getattr(self, f"ret_{n}d", None)


def pct(x: float) -> str:
    return f"{x:+.2f}%"


def price_text(p: float) -> str:
    return f"{int(p):,}" if float(p).is_integer() else f"{p:,.2f}"


def _ret(now: float | None, then: float | None) -> float | None:
    if now is None or then is None or then <= 0:
        return None
    return round((now / then - 1) * 100, 2)


def build_market_facts(ctx: RunContext) -> list[str]:
    h, reg, facts = ctx.history, ctx.registry, ctx.facts
    last, d5, d20 = h.last, h.ago(5), h.ago(20)
    keys: list[str] = []
    twse_last = h.day_quotes("twse", last)
    taiex = twse_last.indices.get("TAIEX") if twse_last else None
    if taiex:
        s_last = reg.add_day_quotes(twse_last)
        keys.append(facts.add(
            [s_last], f"加權指數 {last:%m/%d} 收 {taiex['close']:,.2f} 點，漲跌 {taiex['chg_points']:+,.2f} 點（{pct(taiex['chg_pct'])}）",
            {"TAIEX": round(taiex["close"], 2), "TAIEX_chg_pct": round(taiex["chg_pct"], 2)}, "tw_market", "market"))
        ctx.market_metrics["taiex_chg_pct"] = taiex["chg_pct"]
        parts, values, srcs = [], {}, [s_last]
        for n, day in ((5, d5), (20, d20)):
            dq = h.day_quotes("twse", day) if day else None
            base = dq.indices.get("TAIEX") if dq else None
            if base:
                r = _ret(taiex["close"], base["close"])
                parts.append(f"較 {day:%m/%d}（{n} 個交易日前）收盤 {base['close']:,.2f} 點變動 {pct(r)}")
                values[f"TAIEX_ret_{n}d_pct"] = r
                srcs.append(reg.add_day_quotes(dq))
                ctx.market_metrics[f"taiex_ret_{n}d"] = r
        if parts:
            keys.append(facts.add(srcs, "加權指數" + "；".join(parts), values, "tw_market", "market"))

    ups = downs = 0
    rets20: list[float] = []
    for m in ctx.metrics.values():
        if m.chg_pct is not None:
            ups += m.chg_pct > 0
            downs += m.chg_pct < 0
        if m.ret_20d is not None:
            rets20.append(m.ret_20d)
    srcs = [reg.add_day_quotes(dq) for dq in (h.day_quotes("twse", last), h.day_quotes("tpex", last)) if dq]
    values = {"up_count": ups, "down_count": downs}
    text = f"150 檔標的 {last:%m/%d} 上漲 {ups} 檔、下跌 {downs} 檔"
    if rets20 and d20:
        med = round(statistics.median(rets20), 2)
        values["median_ret_20d_pct"] = med
        text += f"；近 20 個交易日報酬中位數 {pct(med)}"
        srcs += [reg.add_day_quotes(dq) for dq in (h.day_quotes("twse", d20), h.day_quotes("tpex", d20)) if dq]
        ctx.market_metrics["median_ret_20d"] = med
    ctx.market_metrics["breadth"] = ups / (ups + downs) if ups + downs else 0.5
    keys.append(facts.add(srcs, text, values, "tw_breadth", "market"))
    return keys


def build_stock_metrics(ctx: RunContext) -> dict[str, StockMetrics]:
    h = ctx.history
    last = h.last
    ago = {n: h.ago(n) for n in ctx.settings.strategy.lookbacks}
    out: dict[str, StockMetrics] = {}
    window = h.dates[-21:]
    for code, name in ctx.universe.items():
        q = h.quote(code, last)
        market = h.market_of(code)
        if q is None or market is None:
            continue
        closes = [h.close(code, d) for d in window]
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a and b]
        values = [v for v in (h.quote(code, d) for d in window[1:]) if v and v.value]
        chg_pct = round(q.change / q.prev_close * 100, 2) if q.change is not None and q.prev_close else None
        by_n = {n: _ret(q.close, h.close(code, d)) if d else None for n, d in ago.items()}
        m = out[code] = StockMetrics(
            code=code, name=name, market=market, close=q.close, chg_pct=chg_pct,
            ret_5d=by_n[5], ret_20d=by_n[20],
            avg_value=statistics.fmean(v.value for v in values) if values else None,
            volatility=statistics.pstdev(rets) if len(rets) >= 5 else None,
            ret_60d=by_n[60], fund=ctx.fundamentals.get(code), rets=by_n,
        )
        span = [(d, c) for d in h.dates[-61:] if (c := h.close(code, d))]
        if len(span) >= 20:
            m.high = max(span, key=lambda x: x[1])
            m.low = min(span, key=lambda x: x[1])
            m.from_high = _ret(q.close, m.high[1])
            if m.low[0] < m.high[0]:
                m.low_to_high = _ret(m.high[1], m.low[1])
    return out


def build_stock_facts(ctx: RunContext) -> None:
    """每檔標的一筆價量事實；只有被推論引用的才會出現在 D-Plan。"""
    h, reg = ctx.history, ctx.registry
    last = h.last
    ago = {n: h.ago(n) for n in ctx.settings.strategy.lookbacks}
    for code, m in ctx.metrics.items():
        srcs = [reg.add_day_quotes(h.day_quotes(m.market, last))]
        text = f"{code} {m.name} {last:%m/%d} 收盤 {price_text(m.close)} 元"
        values: dict[str, float] = {"close": m.close}
        if m.chg_pct is not None:
            text += f"，當日 {pct(m.chg_pct)}"
            values["chg_pct"] = m.chg_pct
        for n, day in ago.items():
            r = m.ret(n)
            if r is None or day is None:
                continue
            base = h.close(code, day)
            text += f"；較 {day:%m/%d} 收盤 {price_text(base)} 元變動 {pct(r)}（{n} 個交易日）"
            values[f"ret_{n}d_pct"] = r
            srcs.append(reg.add_day_quotes(h.day_quotes(m.market, day)))
        if m.high and m.low and m.from_high is not None:
            (hd, hp), (ld, lp) = m.high, m.low
            text += (f"；近 {min(60, len(h.dates) - 1)} 個交易日最高收盤 {price_text(hp)} 元（{hd:%m/%d}）、"
                     f"最低收盤 {price_text(lp)} 元（{ld:%m/%d}），目前距最高收盤 {pct(m.from_high)}")
            values |= {"high_close": hp, "low_close": lp, "from_high_pct": m.from_high}
            if m.low_to_high is not None:
                text += f"，低點到高點上漲 {pct(m.low_to_high)}"
                values["low_to_high_pct"] = m.low_to_high
            # 只引用區間起訖日（各股共用）：逐檔引用高低點當天的行情會讓 D-Plan 來源超過 50 筆上限
            start = h.dates[-min(61, len(h.dates))]
            srcs.append(reg.add_day_quotes(h.day_quotes(m.market, start)))
        m.fact = ctx.facts.add(list(dict.fromkeys(srcs))[:10], text, values, "price_momentum", code)
        if m.fund is not None:
            m.fund_fact = m.fund.add_fact(ctx, m.name)
