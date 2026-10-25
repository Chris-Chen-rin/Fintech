"""多日演練：用過去的真實行情連續跑多天，驗證第二天以後的持股調整。

每天：產生資料包 → 完整流程（Agent → 風控 → 檢查器）→ 以交易日收盤價成交（含手續費、證交稅）→ 下一天的資料包。
注意：本益比、月營收等「只有最新一期」的資料用的是現在抓得到的版本，早期日子會有些微前視偏差；
演練目的是檢查流程與規則行為，不是評估績效。
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from dplan_checker import rules as R

from .datapack import first_day_datapack
from .run_daily import RunOptions, _universe_markets, run_daily
from .settings import Paths, Settings
from .tools.http import HttpClient
from .tools.market_data import MarketHistory, PriceStore, load_history
from .trading_calendar import TPE, TradingCalendar, iso_tpe

INITIAL_NAV = 1_000_000_000.0
IGNORED_ERRORS = {"TRADE_DATE_OUT_OF_PERIOD"}  # 演練日期在初賽期間之前


@dataclass
class Book:
    cash: float = INITIAL_NAV
    shares: dict[str, int] = field(default_factory=dict)

    def nav(self, prices: dict[str, float]) -> float:
        return self.cash + sum(s * prices[c] for c, s in self.shares.items())


def fill_orders(book: Book, orders: list[dict[str, Any]], prices: dict[str, float]) -> tuple[str, float]:
    """先賣後買、以收盤價成交；買進資金不足時整批作廢（比照規則）。回傳 (結果, 交易成本)。"""
    cash, shares, cost = book.cash, dict(book.shares), 0.0
    for o in sorted(orders, key=lambda o: o["side"] != "SELL"):
        amount = o["shares"] * prices[o["ticker"]]
        if o["side"] == "SELL":
            fee = amount * (R.FEE_RATE + R.TAX_RATE)
            cash += amount - fee
            shares[o["ticker"]] = shares.get(o["ticker"], 0) - o["shares"]
        else:
            fee = amount * R.FEE_RATE
            cash -= amount + fee
            shares[o["ticker"]] = shares.get(o["ticker"], 0) + o["shares"]
        cost += fee
    if cash < 0 or any(s < 0 for s in shares.values()):
        return "void", 0.0
    book.cash, book.shares = cash, {c: s for c, s in shares.items() if s > 0}
    return "executed", cost


def build_datapack(team_id: str, book: Book, trade_date: dt.date, as_of: dt.date, prices: dict[str, float],
                   outcome: str) -> dict[str, Any]:
    nav = book.nav(prices)
    raw = first_day_datapack(team_id, trade_date, iso_tpe(dt.datetime.combine(as_of, dt.time(19, 0), TPE)))
    raw["as_of_date"] = as_of.isoformat()
    raw["prev_day_orders"]["outcome"] = outcome
    raw["account"] = {"cash": book.cash, "cash_ratio_tradable": book.cash / nav, "nav_tradable": nav, "aum": nav,
                      "receivable_cash": 0.0, "holdings_count": len(book.shares)}
    raw["holdings"] = [{"code": c, "shares": s, "receivable_shares": 0, "close_price": prices[c],
                        "tradable_weight": s * prices[c] / nav} for c, s in sorted(book.shares.items())]
    incidents = []
    for c, s in book.shares.items():
        w = s * prices[c] / nav
        if w > R.weight_cap(c):
            incidents.append({"date": as_of.isoformat(), "check_code": "WEIGHT_CAP", "status": "warn",
                              "message": f"{c} 權重 {w:.2%} 超過上限 {R.weight_cap(c):.0%}"})
    raw["compliance"]["incidents"] = incidents
    return raw


def rehearsal_dir(settings: Settings, start: dt.date, end: dt.date) -> Path:
    """演練有自己的 archive／out，不會被正式流程當成「前一交易日」的 D-Plan。"""
    return settings.paths.root / "rehearsals" / f"{start:%Y%m%d}_{end:%Y%m%d}"


def rehearse(settings: Settings, start: dt.date, end: dt.date, offline: bool = False, folder: Path | None = None,
             announcements: bool = True) -> list[dict[str, Any]]:
    cal = TradingCalendar(settings.holidays)
    universe, markets = _universe_markets()
    store = PriceStore(HttpClient(settings.paths.cache, offline=offline), settings.paths.cache, universe, markets)
    n = sum(1 for i in range((end - start).days + 1) if cal.is_trading_day(start + dt.timedelta(days=i))) + 2
    history: MarketHistory = load_history(store, end, n, cal.is_trading_day)
    days = [d for d in history.dates if d >= start]
    if len(days) < 1 or history.dates.index(days[0]) == 0:
        raise ValueError("演練起日之前至少要有一個交易日的行情")
    span = (end - history.dates[0]).days
    closed = {d for i in range(span + 1) if (d := history.dates[0] + dt.timedelta(days=i)).weekday() < 5
              and d not in history.dates}
    folder = folder or rehearsal_dir(settings, start, end)
    if folder.exists():
        shutil.rmtree(folder)
    settings = replace(settings, holidays=frozenset(settings.holidays | closed),
                       paths=Paths(folder, cache_root=settings.paths.cache))
    settings.paths.ensure()

    book, outcome, rows = Book(), "none", []
    taiex0 = history.day_quotes("twse", history.dates[history.dates.index(days[0]) - 1]).indices["TAIEX"]["close"]
    for day in days:
        prev = history.dates[history.dates.index(day) - 1]
        prev_prices = history.closes(prev)
        if not book.shares and outcome == "none":
            raw = first_day_datapack(settings.team_id, day, iso_tpe(dt.datetime.combine(prev, dt.time(19, 0), TPE)))
        else:
            raw = build_datapack(settings.team_id, book, day, prev, prev_prices, outcome)
        dp_path = folder / f"datapack_{day:%Y%m%d}.json"
        dp_path.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n════ 演練 {day}（資料截至 {prev}）════")
        row: dict[str, Any] = {"trade_date": day.isoformat()}
        try:
            result = run_daily(settings, RunOptions(datapack_path=dp_path, offline=offline, write_out=False,
                                                    now=dt.datetime.combine(prev, dt.time(20, 0), TPE),
                                                    ignore_errors=IGNORED_ERRORS, announcements=announcements))
        except Exception as exc:  # noqa: BLE001 - 演練要記下失敗原因並繼續
            row.update(ok=False, error=str(exc))
            result = None
        orders: list[dict[str, Any]] = []
        if result is not None:
            errs = [i for i in result.report.errors if i.code not in IGNORED_ERRORS]
            ok = result.ok
            row.update(ok=ok, errors=len(errs), error_codes=sorted({i.code for i in errs}),
                       warnings=len(result.report.warnings),
                       warning_codes=sorted({i.code for i in result.report.warnings}), agents=result.agents_used)
            if ok:
                doc = json.loads(result.dplan_path.read_text(encoding="utf-8"))
                orders = doc["orders"]
                row.update(intent=doc["market_view"]["posture"]["net_exposure_intent"],
                           decisions=len(doc["decisions"]), no_trades=len(doc["no_trade_decisions"]),
                           actions={a: sum(d["action"] == a for d in doc["decisions"])
                                    for a in ("BUY", "ADD", "TRIM", "SELL_ALL")})
        prices = history.closes(day)
        nav_before = book.nav(prices)
        traded = sum(o["shares"] * prices[o["ticker"]] for o in orders)
        outcome, cost = fill_orders(book, orders, prices) if orders else ("none", 0.0)
        nav = book.nav(prices)
        taiex = history.day_quotes("twse", day).indices["TAIEX"]["close"]
        row.update(outcome=outcome, orders=len(orders), turnover=round(traded / nav_before, 4) if outcome == "executed" else 0.0,
                   cost=round(cost), nav=round(nav), cash_ratio=round(book.cash / nav, 4), holdings=len(book.shares),
                   ret_pct=round((nav / INITIAL_NAV - 1) * 100, 2), taiex_ret_pct=round((taiex / taiex0 - 1) * 100, 2))
        rows.append(row)
    (folder / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return rows


def format_rows(rows: list[dict[str, Any]]) -> str:
    lines = ["交易日      檢查   姿態      決策(買/加/減/清)  續抱  委託  周轉率   成本        現金比   持股  累積報酬  加權指數"]
    for r in rows:
        if not r.get("ok"):
            lines.append(f"{r['trade_date']}  ✗ {r.get('error') or '錯誤：' + '、'.join(r.get('error_codes') or [])}")
            continue
        a = r.get("actions", {})
        lines.append(f"{r['trade_date']}  ✓{r['warnings']:>2}警  {r.get('intent', '-'):<8}  "
                     f"{a.get('BUY', 0):>3}/{a.get('ADD', 0):>2}/{a.get('TRIM', 0):>2}/{a.get('SELL_ALL', 0):>2}       "
                     f"{r.get('no_trades', 0):>3}  {r['orders']:>4}  {r['turnover']:>6.1%}  {r['cost']:>10,}  "
                     f"{r['cash_ratio']:>6.1%}  {r['holdings']:>4}  {r['ret_pct']:>+7.2f}%  {r['taiex_ret_pct']:>+7.2f}%")
    return "\n".join(lines)
