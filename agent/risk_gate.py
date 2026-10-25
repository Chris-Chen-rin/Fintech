"""M7 風控閘門：投資組合經理的決策 → 通過，或違規清單（退回 Agent 修正，不由程式代為決定）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dplan_checker import rules as R
from dplan_checker.portfolio import target_shares

from .datapack import DataPack
from .framework.contracts import MarketView, PortfolioPlan
from .orders import DerivedOrder, derive, simulate_orders
from .settings import Settings


@dataclass(frozen=True)
class Violation:
    code: str
    severity: str  # error / warn
    message: str
    ticker: str | None = None

    def __str__(self) -> str:
        return f"{self.code}：{self.message}"


@dataclass
class GateResult:
    violations: list[Violation]
    orders: list[DerivedOrder]
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors


def check_plan(plan: PortfolioPlan, mv: MarketView, datapack: DataPack, prices: dict[str, float],
               settings: Settings, trade_date_streaks: dict[str, int] | None = None,
               theme_members: set[str] | None = None) -> GateResult:
    book, nav = datapack.book, datapack.book.nav
    risk = settings.risk
    v: list[Violation] = []

    def err(code: str, msg: str, t: str | None = None) -> None:
        v.append(Violation(code, "error", msg, t))

    def warn(code: str, msg: str, t: str | None = None) -> None:
        v.append(Violation(code, "warn", msg, t))

    for d in plan.decisions:
        if d.ticker not in prices:
            err("PRICE_MISSING", f"{d.ticker} 沒有前日收盤價，無法推導委託", d.ticker)
    if any(x.code == "PRICE_MISSING" for x in v):
        return GateResult(v, [])

    for d in plan.decisions:
        t, held_shares, price = d.ticker, book.shares(d.ticker), prices[d.ticker]
        if d.action == "BUY" and held_shares > 0:
            err("ACTION_BUY_BUT_HELD", f"{t} 已持有 {held_shares:,} 股，應為 ADD", t)
        if d.action in ("ADD", "TRIM", "SELL_ALL") and held_shares == 0:
            err("ACTION_NOT_HELD", f"{t} 沒有持股，不能 {d.action}", t)
        exact, floating = target_shares(d.target_weight, nav, price)
        if exact != floating:
            err("FLOAT_EDGE", f"{t} 目標權重 {d.target_weight} 落在整張邊界（精確 {exact:,}、浮點 {floating:,} 股），請微調", t)
        delta = exact - held_shares
        if delta == 0:
            err("DECISION_NO_ORDER", f"{t} 的決策推導不出委託；不調整的持股應列為續抱", t)
        elif d.action == "BUY" and exact == 0:
            err("BUY_ZERO_SHARES", f"{t} 目標權重 {d.target_weight} 買不到一張", t)
        elif d.action == "ADD" and delta < 0 or d.action == "TRIM" and delta > 0:
            err("ACTION_DIRECTION", f"{t} 為 {d.action}，推導結果方向相反", t)
        elif d.action == "TRIM" and exact == 0:
            err("TRIM_TO_ZERO", f"{t} 減碼後為 0 股，應為 SELL_ALL", t)
        elif d.action == "SELL_ALL" and exact != 0:
            err("SELL_ALL_REMAINS", f"{t} 為 SELL_ALL 但推導後仍有 {exact:,} 股；target_weight 應為 0", t)
        cap = R.weight_cap(t)
        if d.action in R.BUY_ACTIONS:
            if d.target_weight > cap - R.WEIGHT_CAP_BUFFER:
                err("TARGET_NEAR_CAP", f"{t} 目標權重 {d.target_weight:.2%} 距規則上限 {cap:.0%} 不到 0.5%", t)
            elif d.target_weight > risk.weight_cap(t):
                warn("TARGET_OVER_INTERNAL_CAP", f"{t} 目標權重 {d.target_weight:.2%} 超過內部上限 {risk.weight_cap(t):.0%}", t)

    orders = derive(plan.decisions, book, prices)
    sim = simulate_orders(book, orders, prices)
    gross, net_ratio = sim.cash_ratio_gross, sim.cash_ratio
    lo, hi = mv.cash_range

    count = sim.holdings_count
    if not R.MIN_HOLDINGS <= count <= R.MAX_HOLDINGS:
        err("HOLDINGS_COUNT", f"交易後持股 {count} 檔，規則為 20–30 檔")
    elif not risk.min_holdings <= count <= risk.max_holdings:
        warn("HOLDINGS_INTERNAL", f"交易後持股 {count} 檔，超出內部目標 {risk.min_holdings}–{risk.max_holdings} 檔")

    buy_tickers = {d.ticker for d in plan.decisions if d.action in R.BUY_ACTIONS}
    streaks = trade_date_streaks or {}
    for code, shares in sim.shares_after.items():
        if shares <= 0:
            continue
        w, cap = sim.weight(code), R.weight_cap(code)
        if code in buy_tickers and w > cap - R.WEIGHT_CAP_BUFFER:
            err("WEIGHT_NEAR_CAP_BY_BUY", f"{code} 加碼後權重 {w:.2%} 距上限 {cap:.0%} 不到 0.5%", code)
        elif code not in buy_tickers and w > cap:
            if streaks.get(code, 0) + 1 > R.PASSIVE_OVERWEIGHT_GRACE_DAYS:
                err("PASSIVE_OVERWEIGHT_DEADLINE", f"{code} 已連續超限 {streaks.get(code, 0)} 天，今天必須減碼到 {cap:.0%} 以下", code)
            else:
                warn("PASSIVE_OVERWEIGHT", f"{code} 權重 {w:.2%} 超過上限 {cap:.0%}（被動超限）", code)

    if not lo <= gross <= hi:
        err("CASH_OUT_OF_RANGE", f"交易後現金比 {gross:.2%} 不在宣告區間 {lo:.0%}–{hi:.0%}")
    elif not lo <= net_ratio <= hi:
        warn("CASH_AFTER_FEES", f"扣費後現金比 {net_ratio:.2%} 落在宣告區間外")
    if sim.cash_after < 0 or net_ratio < R.CASH_EDGE_BUFFER or net_ratio > R.CASH_MAX - R.CASH_EDGE_BUFFER:
        err("CASH_NEAR_LIMIT", f"扣費後現金比 {net_ratio:.2%}，離 0% 或 25% 不到 1%")
    stressed = simulate_orders(book, orders, prices, R.STRESS_PRICE_MOVE, -R.STRESS_PRICE_MOVE)
    if stressed.cash_after < 0:
        err("CASH_STRESS_NEGATIVE", "買價高 3%、賣價低 3% 時現金會變負數")
    relaxed = simulate_orders(book, orders, prices, -R.STRESS_PRICE_MOVE, R.STRESS_PRICE_MOVE)
    if relaxed.cash_ratio > R.CASH_MAX:
        err("CASH_STRESS_OVER", f"買價低 3%、賣價高 3% 時現金比會升到 {relaxed.cash_ratio:.2%}")

    net = sim.net_flow
    if mv.intent == "increase" and not net > 0:
        err("POSTURE_NET_FLOW", f"宣告 increase，但淨流向 {net:,.0f}")
    elif mv.intent == "reduce" and not net < 0:
        err("POSTURE_NET_FLOW", f"宣告 reduce，但淨流向 {net:,.0f}")
    elif mv.intent == "hold" and abs(net) / nav > R.HOLD_NET_FLOW_WARN:
        err("POSTURE_NET_FLOW", f"宣告 hold，但淨買賣差占淨值 {abs(net) / nav:.2%}（上限 2%，內部 1.8%）")

    tagged = {i for i, d in enumerate(plan.decisions) if d.funding_for}
    if tagged:
        without = [o for o in orders if o.decision_index not in tagged]
        if simulate_orders(book, without, prices).cash_ratio_gross >= lo:
            err("FUNDING_NO_GAP", "拿掉資金調度賣出後現金仍不低於宣告下限，不應標記 funding_for")
    elif any(o.side == "SELL" for o in orders) and any(o.side == "BUY" for o in orders):
        buys_only = [o for o in orders if o.side == "BUY"]
        if simulate_orders(book, buys_only, prices).cash_ratio_gross < lo:
            warn("FUNDING_UNTAGGED", "不賣出的話現金會低於宣告下限；資金調度賣出應標記 funding_for")

    theme = theme_members or set()
    theme_weight = sum(sim.weight(c) for c, s in sim.shares_after.items() if s > 0 and c in theme)
    if theme and theme_weight < settings.theme.min_weight:
        warn("THEME_WEIGHT_LOW", f"主題標的占淨值 {theme_weight:.1%}，低於下限 {settings.theme.min_weight:.0%}")

    summary = {
        "orders": len(orders), "buy_amount": round(sim.buy_amount), "sell_amount": round(sim.sell_amount),
        "net_flow_pct": round(net / nav * 100, 3), "cash_ratio_gross": round(gross, 5), "cash_ratio_net": round(net_ratio, 5),
        "holdings_after": count, "fees": round(sim.fees), "theme_weight": round(theme_weight, 4),
        "max_weight": max(((c, round(sim.weight(c), 4)) for c, s in sim.shares_after.items() if s > 0), key=lambda x: x[1], default=None),
    }
    return GateResult(v, orders, summary)


def overweight_streaks(datapack: DataPack, prev_trading_day) -> dict[str, int]:
    """依資料包異常提示，各檔連續權重超限的交易日數（截至 as_of_date）。"""
    if datapack.as_of_date is None:
        return {}
    out: dict[str, int] = {}
    for code in datapack.book.held_codes:
        dates = datapack.overweight_warn_dates(code)
        day, n = datapack.as_of_date, 0
        while day in dates:
            n += 1
            day = prev_trading_day(day)
        if n:
            out[code] = n
    return out
