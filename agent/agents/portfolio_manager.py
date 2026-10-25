"""A5 投資組合經理 Agent：市場判斷＋個股推論 → 買賣決策與目標權重。"""
from __future__ import annotations

import statistics
from typing import Any

from dplan_checker import rules as R
from dplan_checker.portfolio import target_shares

from ..framework.context import RunContext
from ..framework.contracts import Decision, MarketView, NoTrade, PortfolioPlan, Thesis
from ..orders import derive, safe_weight, simulate_orders

CONSTRUCTION = "T:pm:construction"
FUNDING = "T:pm:funding"


def waterfill(raw: dict[str, float], total: float, caps: dict[str, float]) -> dict[str, float]:
    """依 raw 比例分配 total，超過上限的固定在上限，多出的再分給其他檔。"""
    out: dict[str, float] = {}
    free, remaining = dict(raw), total
    while free:
        s = sum(free.values())
        trial = {t: remaining * r / s for t, r in free.items()}
        over = [t for t, x in trial.items() if x > caps[t]]
        if not over:
            out.update(trial)
            break
        for t in over:
            out[t] = caps[t]
            remaining -= caps[t]
            free.pop(t)
    return out


class RulePortfolioManagerAgent:
    """分數排序選股、分數傾斜加權、無交易帶、每日換股上限；依現金目標反覆校正。"""
    name = "portfolio_manager"
    variant = "rule"
    MAX_SWAPS = 3
    TILT = 0.25

    def run(self, ctx: RunContext, inputs: dict[str, Any], feedback: list[str] | None = None) -> PortfolioPlan:
        mv: MarketView = inputs["market_view"]
        theses: dict[str, Thesis] = {t.tickers[0]: t for t in inputs["theses"] if len(t.tickers) == 1}
        book, nav, prices = ctx.book, ctx.book.nav, ctx.prices
        risk = ctx.settings.risk
        held = ctx.held
        theme = set(ctx.settings.theme.tickers)
        retry = len(feedback or [])
        margin = 0.005 * min(retry, 4)
        lo, hi = mv.cash_range

        def score(t: str) -> float:
            th = theses.get(t)
            return th.score if th and th.score is not None else -9.0

        ranked = sorted(theses, key=lambda t: -score(t))
        rank = {t: i + 1 for i, t in enumerate(ranked)}
        n_target = round((risk.min_holdings + risk.max_holdings) / 2)

        drops: list[str] = []
        if held:
            weak = [t for t in held if t in theses and theses[t].view == "negative" and rank[t] > n_target * 1.5]
            drops = sorted(weak, key=score)[:self.MAX_SWAPS]
        keep = [t for t in held if t not in drops]
        if len(keep) > risk.max_holdings:
            extra = sorted(keep, key=score)[:len(keep) - risk.max_holdings]
            drops += extra
            keep = [t for t in keep if t not in extra]

        current_cash = book.cash / nav
        cash_goal = mv.cash_target if mv.intent != "hold" else min(max(current_cash, lo + 0.005), hi - 0.005)
        equity = 1 - cash_goal
        base_weight = equity / n_target

        def affordable(t: str) -> bool:
            return t in prices and prices[t] * 1000 <= 0.5 * base_weight * nav

        pool = [t for t in ranked if t not in held and affordable(t) and theses[t].view == "positive"]
        pool.sort(key=lambda t: (bool(theme) and t not in theme, -score(t)))
        new = pool[:max(0, n_target - len(keep))]
        if len(keep) + len(new) < risk.min_holdings:
            backup = [t for t in ranked if t not in held and t not in new and affordable(t) and theses[t].view == "neutral"]
            new += backup[:risk.min_holdings - len(keep) - len(new)]
        selected = keep + new

        scores = {t: score(t) for t in selected if t in theses}
        mu = statistics.fmean(scores.values()) if scores else 0.0
        sd = (statistics.pstdev(scores.values()) if len(scores) > 1 else 0.0) or 1.0
        raw = {t: 1 + self.TILT * max(-1.5, min(1.5, (scores.get(t, mu) - mu) / sd)) for t in selected if t in prices}
        caps = {t: risk.weight_cap(t) - margin for t in raw}

        w_cur = {t: book.shares(t) * prices[t] / nav for t in held if t in prices}
        ceiling = hi - 0.003
        if mv.intent == "hold":
            ceiling = min(ceiling, current_cash + 0.015)
        alpha = 0.7 ** retry  # 既有持股往目標調整的幅度；退回次數越多調整越保守
        plan, goal = None, cash_goal
        for _ in range(40):
            base = waterfill(raw, equity, caps)
            weights = {t: w_cur[t] + alpha * (w - w_cur[t]) if t in w_cur and w_cur[t] <= caps[t] else w
                       for t, w in base.items()}
            plan = self._build(ctx, mv, theses, weights, selected, drops)
            sim = simulate_orders(book, derive(plan.decisions, book, prices), prices)
            turnover = (sim.buy_amount + sim.sell_amount) / nav
            # 成交均價可能比前日收盤差 3%：現金要留得住這個缺口，否則整批委託會因資金不足作廢
            goal = max(cash_goal, R.STRESS_PRICE_MOVE * turnover + sim.fees / nav + 0.012, lo + 0.003)
            if goal > ceiling and alpha > 0.05:
                alpha *= 0.7
                continue
            gap = sim.cash_ratio_gross - (goal + 0.0015)
            if abs(gap) < 0.0025:
                break
            equity = min(sum(caps.values()), max(0.0, equity + gap))
        assert plan is not None
        self._tag_funding(ctx, mv, plan, theses)
        plan.notes = {"n_target": n_target, "selected": len(selected), "new": new, "drops": drops,
                      "cash_goal": round(goal, 4), "equity_target": round(equity, 4), "alpha": round(alpha, 3), "retry": retry}
        return plan

    def _build(self, ctx: RunContext, mv: MarketView, theses: dict[str, Thesis], weights: dict[str, float],
               selected: list[str], drops: list[str]) -> PortfolioPlan:
        book, nav, prices, risk = ctx.book, ctx.book.nav, ctx.prices, ctx.settings.risk
        band = risk.no_trade_band
        decisions: list[Decision] = []
        no_trades: list[NoTrade] = []
        construction = self._construction_thesis(ctx, mv, len(selected), len(theses))
        for t in sorted(set(selected) | ctx.held, key=lambda c: (c not in ctx.held, c)):
            price = prices.get(t)
            refs = [f"T:{t}"] if t in theses else []
            current = book.shares(t)
            w_cur = current * price / nav if price else 0.0
            if price is None:
                no_trades.append(NoTrade(t, refs + [CONSTRUCTION], "缺少前日收盤價，今日無法推導委託，維持不動"))
                continue
            target = weights.get(t, 0.0) if t not in drops else 0.0
            if t in ctx.held and t in selected and abs(target - w_cur) < band and w_cur <= risk.weight_cap(t):
                no_trades.append(NoTrade(t, refs or [CONSTRUCTION],
                                         f"權重 {w_cur:.2%} 與目標 {target:.2%} 差距小於無交易帶 {band:.1%}，維持不動"))
                continue
            tw = safe_weight(target, nav, price) if target > 0 else 0.0
            exact = target_shares(tw, nav, price)[0] if tw > 0 else 0
            delta = exact - current
            if delta == 0:
                if t in ctx.held:
                    no_trades.append(NoTrade(t, refs or [CONSTRUCTION], f"目標權重 {target:.2%} 換算後股數不變，維持不動"))
                continue
            if current == 0:
                action = "BUY"
            elif exact == 0:
                action, tw = "SELL_ALL", 0.0
            else:
                action = "ADD" if delta > 0 else "TRIM"
            cap = risk.weight_cap(t)
            risk_check = (f"目標權重 {tw:.2%}（內部上限 {cap:.0%}、規則上限 {R.weight_cap(t):.0%}）；"
                          f"原權重 {w_cur:.2%}" + ("；動能轉弱且排名落後，出清讓出名額" if t in drops else ""))
            decisions.append(Decision(t, action, tw, (refs + [CONSTRUCTION])[:8], risk_check[:300]))
        return PortfolioPlan(decisions, no_trades, [construction])

    def _construction_thesis(self, ctx: RunContext, mv: MarketView, n_selected: int, n_candidates: int) -> Thesis:
        risk = ctx.settings.risk
        tokens = " ".join(f"{{{k}}}" for k in mv.basis)
        logic = (f"依市場事實 {tokens} 的判斷（{mv.regime}／{mv.stance}），目標現金比 {mv.cash_target:.0%}、"
                 f"宣告區間 {mv.cash_range[0]:.0%}–{mv.cash_range[1]:.0%}。組合規則：從 {n_candidates} 檔候選中依動能分數挑選 "
                 f"{n_selected} 檔，以分數傾斜 ±{self.TILT:.0%} 的等權重配置，2330 上限 {risk.tsmc_max_weight:.0%}、其他個股 "
                 f"{risk.stock_max_weight:.0%}；權重偏離小於 {risk.no_trade_band:.1%} 的持股不調整，每日最多出清 {self.MAX_SWAPS} 檔以控制周轉成本。")
        return Thesis(CONSTRUCTION, [], list(mv.basis), logic[:1000],
                      f"持股 {n_selected} 檔、股票部位約 {1 - mv.cash_target:.0%}", None, "neutral", None, None, "portfolio_manager/rule")

    def _tag_funding(self, ctx: RunContext, mv: MarketView, plan: PortfolioPlan, theses: dict[str, Thesis]) -> None:
        """不賣出就會跌破現金下限時，賣出才是資金調度：標記 funding_for 並寫明讓位理由。"""
        book, prices = ctx.book, ctx.prices
        orders = derive(plan.decisions, book, prices)
        sells = [d for d in plan.decisions if d.action in R.SELL_ACTIONS]
        buys = [o for o in orders if o.side == "BUY"]
        if not sells or not buys:
            return
        if simulate_orders(book, buys, prices).cash_ratio_gross >= mv.cash_range[0]:
            return
        top_buys = [o.ticker for o in sorted(buys, key=lambda o: -o.shares * prices[o.ticker])[:5]]
        sold = [d.ticker for d in sells]
        premises = [ctx.metrics[t].fact for t in sold if t in ctx.metrics and ctx.metrics[t].fact][:10]
        if not premises:
            return
        names = "、".join(ctx.name(t) for t in sold)
        logic = (f"買進 {'、'.join(top_buys)} 等部位需要資金，若不賣出現金比將低於宣告下限 {mv.cash_range[0]:.0%}。"
                 f"選擇 {names} 讓位（" + "、".join(f"{{{p}}}" for p in premises) +
                 "），理由是其動能分數在持股中最低或權重高於目標，減碼對組合影響最小；賣出所得資金用於支應上述買進（資金調度）。")
        plan.theses.append(Thesis(FUNDING, sold, premises, logic[:1000], f"資金調度：賣出 {len(sold)} 檔支應買進",
                                  None, "neutral", None, None, "portfolio_manager/rule"))
        for d in sells:
            d.funding_for = top_buys
            if FUNDING not in d.theses:
                d.theses = (d.theses + [FUNDING])[:8]
