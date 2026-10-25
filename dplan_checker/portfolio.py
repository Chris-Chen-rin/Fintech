from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal

from .rules import FEE_RATE, LOT_SIZE, TAX_RATE


@dataclass
class Position:
    code: str
    shares: int
    receivable_shares: int
    close_price: float | None
    tradable_weight: float | None


@dataclass
class Book:
    cash: float
    nav: float
    positions: dict[str, Position]

    def shares(self, code: str) -> int:
        pos = self.positions.get(code)
        return pos.shares if pos else 0

    @property
    def held_codes(self) -> set[str]:
        return {c for c, p in self.positions.items() if p.shares > 0}


def target_shares(target_weight: float, nav: float, price: float) -> tuple[int, int]:
    """官方公式 floor(target_weight × nav ÷ price ÷ 1000) × 1000。回傳（十進位精確值, 浮點運算值）。"""
    lots = (Decimal(repr(target_weight)) * Decimal(repr(nav)) / Decimal(repr(price)) / LOT_SIZE)
    exact = int(lots.to_integral_value(rounding=ROUND_FLOOR)) * LOT_SIZE
    floating = math.floor(target_weight * nav / price / LOT_SIZE) * LOT_SIZE
    return exact, floating


@dataclass
class Order:
    ticker: str
    side: str
    shares: int
    decision_ref: str | None = None


@dataclass
class Simulation:
    buy_amount: float
    sell_amount: float
    fees: float
    cash_after: float
    cash_after_gross: float
    nav_after: float
    nav_after_gross: float
    shares_after: dict[str, int] = field(default_factory=dict)
    values_after: dict[str, float] = field(default_factory=dict)

    @property
    def net_flow(self) -> float:
        return self.buy_amount - self.sell_amount

    @property
    def cash_ratio(self) -> float:
        return self.cash_after / self.nav_after if self.nav_after > 0 else float("nan")

    @property
    def cash_ratio_gross(self) -> float:
        return self.cash_after_gross / self.nav_after_gross if self.nav_after_gross > 0 else float("nan")

    def weight(self, code: str) -> float:
        return self.values_after.get(code, 0.0) / self.nav_after if self.nav_after > 0 else float("nan")

    @property
    def holdings_count(self) -> int:
        return sum(1 for s in self.shares_after.values() if s > 0)

    def adverse_move_tolerance(self, cash_before: float) -> float:
        """買進價上漲、賣出價下跌各 x 時現金恰好歸零的 x。"""
        buy_cost = self.buy_amount * (1 + FEE_RATE)
        sell_net = self.sell_amount * (1 - FEE_RATE - TAX_RATE)
        denom = buy_cost + sell_net
        if denom == 0:
            return float("inf")
        return (cash_before - buy_cost + sell_net) / denom


def trade_cost(side: str, amount: float) -> float:
    return amount * (FEE_RATE + (TAX_RATE if side == "SELL" else 0.0))


def simulate(book: Book, orders: list[Order], price_of: dict[str, float],
             buy_move: float = 0.0, sell_move: float = 0.0) -> Simulation:
    """以前日收盤價（可加減價格變動）模擬全數成交後的部位。價格缺漏的委託不納入。"""
    shares_after = {c: p.shares for c, p in book.positions.items() if p.shares > 0}
    buy_amount = sell_amount = fees = 0.0
    for order in orders:
        price = price_of.get(order.ticker)
        if price is None:
            continue
        if order.side == "BUY":
            amount = order.shares * price * (1 + buy_move)
            buy_amount += amount
            shares_after[order.ticker] = shares_after.get(order.ticker, 0) + order.shares
        else:
            amount = order.shares * price * (1 + sell_move)
            sell_amount += amount
            shares_after[order.ticker] = shares_after.get(order.ticker, 0) - order.shares
        fees += trade_cost(order.side, amount)

    cash_gross = book.cash - buy_amount + sell_amount
    cash_net = cash_gross - fees
    values = {c: s * price_of.get(c, 0.0) for c, s in shares_after.items() if s > 0}
    equity = sum(values.values())
    return Simulation(
        buy_amount=buy_amount,
        sell_amount=sell_amount,
        fees=fees,
        cash_after=cash_net,
        cash_after_gross=cash_gross,
        nav_after=cash_net + equity,
        nav_after_gross=cash_gross + equity,
        shares_after=shares_after,
        values_after=values,
    )


def active_share(portfolio: dict[str, float], benchmark: dict[str, float], top_n: int,
                 normalize: bool = True) -> float:
    """Active Share = ½ Σ|Wp − Wb|，雙方各取前 top_n 大持股。"""
    def top(weights: dict[str, float]) -> dict[str, float]:
        items = sorted(((k, v) for k, v in weights.items() if v > 0), key=lambda kv: kv[1], reverse=True)[:top_n]
        picked = dict(items)
        total = sum(picked.values())
        if normalize and total > 0:
            picked = {k: v / total for k, v in picked.items()}
        return picked

    p, b = top(portfolio), top(benchmark)
    return 0.5 * sum(abs(p.get(k, 0.0) - b.get(k, 0.0)) for k in set(p) | set(b))
