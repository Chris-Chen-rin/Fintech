"""M2 資料包讀取：DataPack → 帳戶與持股（依《DataPack 規格書 v4.3》）。"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dplan_checker.loaders import InputError, load_json
from dplan_checker.portfolio import Book, Position


class DataPackError(Exception):
    """資料包不能用來產生 D-Plan。"""


@dataclass(frozen=True)
class Incident:
    date: str
    check_code: str
    status: str
    message: str


@dataclass
class DataPack:
    path: Path | None
    team_id: str
    trade_date: dt.date
    as_of_date: dt.date | None
    generated_at: str
    missing: tuple[str, ...]
    prev_outcome: str
    void_reasons: tuple[str, ...]
    excluded_codes: tuple[str, ...]
    book: Book
    aum: float
    receivable_cash: float
    violations: int
    notices: int
    incidents: tuple[Incident, ...]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def is_first_day(self) -> bool:
        """尚無結算資料且帳戶是真值（全現金、0 檔）。"""
        return self.as_of_date is None and not self.book.held_codes

    @property
    def cash_ratio(self) -> float:
        return self.book.cash / self.book.nav if self.book.nav else 0.0

    def close_prices(self) -> dict[str, float]:
        return {c: float(p.close_price) for c, p in self.book.positions.items() if p.close_price is not None}

    def weight(self, code: str) -> float:
        pos = self.book.positions.get(code)
        if not pos or not pos.shares or pos.close_price is None or not self.book.nav:
            return 0.0
        return pos.shares * pos.close_price / self.book.nav

    def overweight_warn_dates(self, code: str) -> set[dt.date]:
        """資料包異常提示中，該檔因權重超限被提示的交易日。"""
        dates: set[dt.date] = set()
        for inc in self.incidents:
            if inc.status == "warn" and code in inc.message and ("weight" in inc.check_code.lower() or "權重" in inc.message):
                try:
                    dates.add(dt.date.fromisoformat(inc.date))
                except ValueError:
                    pass
        return dates


def parse_datapack(data: Any, path: Path | None = None, team_id: str | None = None) -> DataPack:
    if not isinstance(data, dict):
        raise DataPackError("資料包最外層必須是 JSON 物件")
    if data.get("doc_type") != "DataPack":
        raise DataPackError(f"doc_type 為 {data.get('doc_type')!r}，不是 DataPack；是否放錯檔案？")
    if team_id and data.get("team_id") != team_id:
        raise DataPackError(f"資料包隊伍 {data.get('team_id')!r} 與設定的隊伍代號 {team_id!r} 不一致")
    try:
        trade_date = dt.date.fromisoformat(str(data.get("trade_date")))
    except ValueError as exc:
        raise DataPackError(f"trade_date {data.get('trade_date')!r} 不是日期") from exc
    as_of_raw = data.get("as_of_date")
    try:
        as_of = dt.date.fromisoformat(as_of_raw) if as_of_raw else None
    except ValueError as exc:
        raise DataPackError(f"as_of_date {as_of_raw!r} 不是日期") from exc

    missing = tuple(str(m) for m in data.get("missing") or [])
    account, holdings = data.get("account"), data.get("holdings")
    if "prev_settlement" in missing or account is None or holdings is None:
        raise DataPackError("前一交易日的結算尚未產生（account / holdings 為 null），請等結算完成後重新下載資料包")

    positions: dict[str, Position] = {}
    for h in holdings:
        code = str(h.get("code"))
        shares = int(h.get("shares") or 0)
        if shares < 0:
            raise DataPackError(f"{code} 的持股為負數 {shares}")
        close = h.get("close_price")
        positions[code] = Position(code, shares, int(h.get("receivable_shares") or 0),
                                   float(close) if close is not None else None, h.get("tradable_weight"))
    try:
        book = Book(float(account["cash"]), float(account["nav_tradable"]), positions)
    except (KeyError, TypeError, ValueError) as exc:
        raise DataPackError(f"account 欄位不完整：{exc}") from exc
    if book.nav <= 0:
        raise DataPackError(f"nav_tradable 為 {book.nav}，無法計算權重")

    prev = data.get("prev_day_orders") or {}
    compliance = data.get("compliance") or {}
    incidents = tuple(
        Incident(str(i.get("date", "")), str(i.get("check_code", "")), str(i.get("status", "")), str(i.get("message", "")))
        for i in compliance.get("incidents") or [] if isinstance(i, dict)
    )
    return DataPack(
        path=path,
        team_id=str(data.get("team_id")),
        trade_date=trade_date,
        as_of_date=as_of,
        generated_at=str(data.get("generated_at") or ""),
        missing=missing,
        prev_outcome=str(prev.get("outcome") or "none"),
        void_reasons=tuple(prev.get("void_reasons") or []),
        excluded_codes=tuple(prev.get("excluded_codes") or []),
        book=book,
        aum=float(account.get("aum") or book.nav),
        receivable_cash=float(account.get("receivable_cash") or 0.0),
        violations=int(compliance.get("violations") or 0),
        notices=int(compliance.get("notices") or 0),
        incidents=incidents,
        raw=data,
    )


def load_datapack(path: Path, team_id: str | None = None) -> DataPack:
    try:
        data = load_json(path).data
    except InputError as exc:
        raise DataPackError(str(exc)) from exc
    return parse_datapack(data, path, team_id)


def find_datapack(folder: Path, trade_date: dt.date) -> Path:
    """在資料夾中找 trade_date 相符的資料包；有多份時取 generated_at 最新的。"""
    candidates: list[tuple[str, Path]] = []
    for p in sorted(folder.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("doc_type") == "DataPack" and data.get("trade_date") == trade_date.isoformat():
            candidates.append((str(data.get("generated_at") or ""), p))
    if not candidates:
        raise DataPackError(f"{folder} 裡沒有 trade_date = {trade_date} 的資料包；請從後台「文件繳交」頁下載後放進來")
    return max(candidates)[1]


def first_day_datapack(team_id: str, trade_date: dt.date, generated_at: str, nav: float = 1_000_000_000.0) -> dict[str, Any]:
    """規格書 §3.2(a) 的首日資料包，用於演練。"""
    return {
        "schema_version": "3.0",
        "doc_type": "DataPack",
        "team_id": team_id,
        "trade_date": trade_date.isoformat(),
        "as_of_date": None,
        "generated_at": generated_at,
        "missing": [],
        "prev_day_orders": {"outcome": "none", "void_reasons": [], "excluded_codes": []},
        "account": {"cash": nav, "cash_ratio_tradable": 1.0, "nav_tradable": nav, "aum": nav,
                    "receivable_cash": 0.0, "holdings_count": 0},
        "holdings": [],
        "compliance": {"violations": 0, "notices": 0, "incidents": []},
    }
