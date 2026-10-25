"""M8 委託推導：官方演算法 floor(target_weight × nav ÷ prev_close ÷ 1000) × 1000 − 現持股數。"""
from __future__ import annotations

from dataclasses import dataclass

from dplan_checker.portfolio import Book, Order, Simulation, simulate, target_shares

from .framework.contracts import Decision


@dataclass(frozen=True)
class DerivedOrder:
    ticker: str
    side: str
    shares: int
    decision_index: int
    detail: str

    def as_order(self) -> Order:
        return Order(self.ticker, self.side, self.shares)


def safe_weight(weight: float, nav: float, price: float, digits: int = 4) -> float:
    """四捨五入到 digits 位，並避開浮點與精確計算張數不同的邊界值（伺服器可能算出不同結果）。"""
    step = 10 ** -digits
    w = round(max(0.0, weight), digits)
    for _ in range(20):
        exact, floating = target_shares(w, nav, price)
        if exact == floating:
            return w
        w = round(w - step, digits)
    return w


def derive(decisions: list[Decision], book: Book, prices: dict[str, float]) -> list[DerivedOrder]:
    out: list[DerivedOrder] = []
    for i, d in enumerate(decisions):
        price = prices[d.ticker]
        exact, _ = target_shares(d.target_weight, book.nav, price)
        current = book.shares(d.ticker)
        delta = exact - current
        if delta == 0:
            continue
        detail = (f"target_shares = floor({d.target_weight} × {book.nav:,.0f} / {price:g} / 1000) × 1000 = {exact:,}；"
                  f"現持 {current:,} → {'BUY' if delta > 0 else 'SELL'} {abs(delta):,}")
        out.append(DerivedOrder(d.ticker, "BUY" if delta > 0 else "SELL", abs(delta), i, detail[:300]))
    return out


def simulate_orders(book: Book, orders: list[DerivedOrder], prices: dict[str, float],
                    buy_move: float = 0.0, sell_move: float = 0.0) -> Simulation:
    return simulate(book, [o.as_order() for o in orders], prices, buy_move, sell_move)
