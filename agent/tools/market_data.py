"""M3 資料工具（行情）：證交所、櫃買中心的每日全市場收盤行情。

- 上市：https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=YYYYMMDD&type=ALLBUT0999&response=json
  （含所有上市股票收盤與各指數）
- 上櫃：https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?date=YYYY/MM/DD&response=json
只保留 150 檔標的與加權指數，整理後存在 cache/prices/，原始回應另存 cache/raw/ 作為證據。
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .http import FetchError, HttpClient, atomic_write

TWSE_DAY_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={ymd}&type=ALLBUT0999&response=json"
TPEX_DAY_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?date={y}%2F{m}%2F{d}&response=json"
TAIEX_NAME = "發行量加權股價指數"
PARSER_VERSION = 2  # 解析邏輯改變時加一，已整理的快取會從原始回應重新解析
CLOSE_TIME = dt.time(13, 30)


@dataclass
class Quote:
    close: float
    change: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    value: float | None = None
    avg_price: float | None = None

    @property
    def prev_close(self) -> float | None:
        return self.close - self.change if self.change is not None else None


@dataclass
class DayQuotes:
    market: str
    date: dt.date
    url: str
    fetched_at: str
    snapshot: str
    quotes: dict[str, Quote] = field(default_factory=dict)
    indices: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["date"] = self.date.isoformat()
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> DayQuotes:
        return cls(d["market"], dt.date.fromisoformat(d["date"]), d["url"], d["fetched_at"], d["snapshot"],
                   {k: Quote(**v) for k, v in d["quotes"].items()}, d.get("indices") or {})


def num(text: Any) -> float | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = re.sub(r"<[^>]+>", "", str(text)).replace(",", "").strip()
    if s in ("", "--", "---", "----", "除權息", "除息", "除權"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _sign(text: Any) -> float:
    s = re.sub(r"<[^>]+>", "", str(text or "")).strip()
    return -1.0 if s.startswith("-") else 1.0


def twse_url(day: dt.date) -> str:
    return TWSE_DAY_URL.format(ymd=f"{day:%Y%m%d}")


def tpex_url(day: dt.date) -> str:
    return TPEX_DAY_URL.format(y=day.year, m=f"{day:%m}", d=f"{day:%d}")


def parse_twse(data: Any, day: dt.date, codes: set[str]) -> tuple[dict[str, Quote], dict[str, dict[str, float]]] | None:
    if not isinstance(data, dict) or data.get("stat") != "OK" or str(data.get("date")) != f"{day:%Y%m%d}":
        return None
    quotes: dict[str, Quote] = {}
    indices: dict[str, dict[str, float]] = {}
    for table in data.get("tables") or []:
        fields = table.get("fields") or []
        rows = table.get("data") or []
        if "證券代號" in fields and "收盤價" in fields:
            ix = {f: i for i, f in enumerate(fields)}
            for row in rows:
                code = str(row[ix["證券代號"]]).strip()
                if code not in codes:
                    continue
                close = num(row[ix["收盤價"]])
                if close is None:
                    continue
                diff = num(row[ix["漲跌價差"]])
                quotes[code] = Quote(
                    close=close,
                    change=_sign(row[ix["漲跌(+/-)"]]) * abs(diff) if diff is not None else None,
                    open=num(row[ix["開盤價"]]), high=num(row[ix["最高價"]]), low=num(row[ix["最低價"]]),
                    volume=num(row[ix["成交股數"]]), value=num(row[ix["成交金額"]]),
                )
        elif "收盤指數" in fields and "價格指數" in str(table.get("title") or ""):
            ix = {f: i for i, f in enumerate(fields)}
            for row in rows:
                if str(row[0]).strip() == TAIEX_NAME:
                    sign = _sign(row[ix["漲跌(+/-)"]])
                    indices["TAIEX"] = {"close": num(row[ix["收盤指數"]]) or 0.0,
                                        "chg_pct": sign * abs(num(row[ix["漲跌百分比(%)"]]) or 0.0),
                                        "chg_points": sign * abs(num(row[ix["漲跌點數"]]) or 0.0)}
    if not quotes:
        return None
    return quotes, indices


def parse_tpex(data: Any, day: dt.date, codes: set[str]) -> dict[str, Quote] | None:
    if not isinstance(data, dict) or str(data.get("date")) != f"{day:%Y%m%d}":
        return None
    quotes: dict[str, Quote] = {}
    for table in data.get("tables") or []:
        fields = table.get("fields") or []
        if "代號" not in fields or "收盤" not in fields:
            continue
        ix = {f: i for i, f in enumerate(fields)}
        for row in table.get("data") or []:
            code = str(row[ix["代號"]]).strip()
            if code not in codes:
                continue
            close = num(row[ix["收盤"]])
            if close is None:
                continue
            quotes[code] = Quote(
                close=close, change=num(row[ix["漲跌"]]),
                open=num(row[ix["開盤"]]), high=num(row[ix["最高"]]), low=num(row[ix["最低"]]),
                volume=num(row[ix["成交股數"]]), value=num(row[ix["成交金額(元)"]]), avg_price=num(row[ix["均價"]]),
            )
    return quotes or None


class PriceStore:
    """每日整理後的行情快取；歷史日不重抓，非交易日會記為休市。"""

    def __init__(self, client: HttpClient, cache_dir: Path, universe: dict[str, str], markets: dict[str, str]):
        self.client = client
        self.dir = cache_dir / "prices"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.universe = universe
        self.codes = {"twse": {c for c, m in markets.items() if m == "twse"},
                      "tpex": {c for c, m in markets.items() if m == "tpex"}}

    def _path(self, market: str, day: dt.date) -> Path:
        return self.dir / f"{market}_{day.isoformat()}.json"

    def cached(self, market: str, day: dt.date) -> DayQuotes | None | bool:
        """DayQuotes；False 表示已知休市；None 表示尚未抓。"""
        p = self._path(market, day)
        if not p.exists():
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
        if d.get("closed"):
            return False
        return DayQuotes.from_json(d) if d.get("parser") == PARSER_VERSION else None

    def mark_closed(self, day: dt.date) -> None:
        for market in ("twse", "tpex"):
            if self.cached(market, day) is not False:
                atomic_write(self._path(market, day), json.dumps({"closed": True, "date": day.isoformat()}))

    def day(self, market: str, day: dt.date) -> DayQuotes | None:
        """取得某市場某日行情；資料不存在（未公布或休市）回傳 None。"""
        hit = self.cached(market, day)
        if hit is False:
            return None
        if hit:
            return hit
        url = twse_url(day) if market == "twse" else tpex_url(day)
        fetched = self.client.get_json(url)
        indices: dict[str, dict[str, float]] = {}
        if market == "twse":
            parsed = parse_twse(fetched.data, day, self.codes["twse"])
            quotes = parsed[0] if parsed else None
            indices = parsed[1] if parsed else {}
        else:
            quotes = parse_tpex(fetched.data, day, self.codes["tpex"])
        if not quotes:
            self.client.forget(url)
            return None
        dq = DayQuotes(market, day, url, fetched.fetched_at, str(fetched.snapshot), quotes, indices)
        atomic_write(self._path(market, day), json.dumps(dq.to_json() | {"parser": PARSER_VERSION}, ensure_ascii=False))
        return dq


@dataclass
class MarketHistory:
    """最近 N 個交易日（舊→新）的上市、上櫃行情。"""
    dates: list[dt.date]
    days: dict[str, dict[dt.date, DayQuotes]]

    @property
    def last(self) -> dt.date:
        return self.dates[-1]

    def market_of(self, code: str) -> str | None:
        for market in ("twse", "tpex"):
            dq = self.days[market].get(self.last)
            if dq and code in dq.quotes:
                return market
        return None

    def quote(self, code: str, day: dt.date) -> Quote | None:
        for market in ("twse", "tpex"):
            dq = self.days[market].get(day)
            if dq and code in dq.quotes:
                return dq.quotes[code]
        return None

    def close(self, code: str, day: dt.date) -> float | None:
        q = self.quote(code, day)
        return q.close if q else None

    def closes(self, day: dt.date | None = None) -> dict[str, float]:
        day = day or self.last
        out: dict[str, float] = {}
        for market in ("twse", "tpex"):
            dq = self.days[market].get(day)
            if dq:
                out.update({c: q.close for c, q in dq.quotes.items()})
        return out

    def ago(self, n: int) -> dt.date | None:
        return self.dates[-1 - n] if len(self.dates) > n else None

    def day_quotes(self, market: str, day: dt.date) -> DayQuotes | None:
        return self.days[market].get(day)


def load_history(store: PriceStore, end: dt.date, n_days: int, is_weekday_open, max_lookback: int | None = None) -> MarketHistory:
    """從 end（前一交易日）往前收集 n_days 個有行情的交易日。end 當天沒有行情就報錯。"""
    dates: list[dt.date] = []
    days: dict[str, dict[dt.date, DayQuotes]] = {"twse": {}, "tpex": {}}
    day = end
    for _ in range(max_lookback or max(140, n_days * 2 + 30)):
        if len(dates) >= n_days:
            break
        if is_weekday_open(day):
            twse = store.day("twse", day)
            tpex = store.day("tpex", day)
            if twse and tpex:
                dates.append(day)
                days["twse"][day], days["tpex"][day] = twse, tpex
            elif day == end:
                missing = "、".join(m for m, d in (("證交所", twse), ("櫃買中心", tpex)) if not d)
                raise FetchError(f"{end} 的{missing}收盤行情還抓不到（尚未公布、休市或網站異常）")
            elif not twse and not tpex:
                store.mark_closed(day)
        day -= dt.timedelta(days=1)
    dates.reverse()
    if len(dates) < 2:
        raise FetchError("可用的歷史行情不足 2 天")
    return MarketHistory(dates, days)
