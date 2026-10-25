"""M3 資料工具（財報）：公開資訊觀測站財報彙總、月營收、本益比與股價淨值比。

- 綜合損益表彙總（累計至該季）：mopsov.twse.com.tw/mops/web/ajax_t163sb04
- 現金流量表彙總（累計至該季）：mopsov.twse.com.tw/mops/web/ajax_t163sb20
- 月營收：mopsov.twse.com.tw/nas/t21/{sii|otc}/t21sc03_{民國年}_{月}_{0 本國|1 外國}.html
- 本益比、殖利率、股價淨值比（按日期）：證交所 BWIBBU_d、櫃買中心 peQryDate
- 公司基本資料（實收資本額、面額 → 發行股數）：t187ap03_L、mopsfin_t187ap03_O

金額單位：財報彙總為千元，事實文字換算成億元。
財報、月營收、公司基本資料的 authority 填 mops；本益比等交易所統計填 twse／tpex。
"""
from __future__ import annotations

import calendar
import datetime as dt
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from ..sources import SourceRecord
from ..trading_calendar import TPE, iso_tpe
from .http import FetchError, HttpClient
from .market_data import CLOSE_TIME

MOPS_URL = ("https://mopsov.twse.com.tw/mops/web/ajax_{page}?encodeURIComponent=1&step=1&firstin=1&off=1"
            "&isQuery=Y&TYPEK={typek}&year={year}&season={season:02d}")
REVENUE_URL = "https://mopsov.twse.com.tw/nas/t21/{typek}/t21sc03_{year}_{month}_{foreign}.html"
PAGES = {"income": "t163sb04", "cashflow": "t163sb20"}
TYPEK = {"twse": "sii", "tpex": "otc"}
MARKET_LABEL = {"twse": "上市", "tpex": "上櫃"}
VALUATION_URL = {
    "twse": "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d?date={ymd}&selectType=ALL&response=json",
    "tpex": "https://www.tpex.org.tw/www/zh-tw/afterTrading/peQryDate?date={y}%2F{m}%2F{d}&response=json",
}
OPENAPI = {
    ("profile", "twse"): "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
    ("profile", "tpex"): "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
}
DEADLINE = {1: (5, 15), 2: (8, 14), 3: (11, 14), 4: (3, 31)}  # 法定公告期限；第 4 季為隔年 3/31
PERIOD_LABEL = {1: "第 1 季", 2: "上半年", 3: "前三季", 4: "全年"}
PARSER_VERSION = 2


@dataclass(frozen=True, order=True)
class Season:
    year: int  # 民國年
    q: int

    def __str__(self) -> str:
        return f"{self.year}Q{self.q}"

    @property
    def end(self) -> dt.date:
        y, m = self.year + 1911, self.q * 3
        return dt.date(y, m, calendar.monthrange(y, m)[1])

    @property
    def deadline(self) -> dt.date:
        m, d = DEADLINE[self.q]
        return dt.date(self.year + 1911 + (self.q == 4), m, d)

    def final(self, as_of: dt.date) -> bool:
        """公告期限過後一週視為定案（之後只有少數更正），可永久快取。"""
        return as_of > self.deadline + dt.timedelta(days=7)

    def prev(self) -> Season:
        return Season(self.year, self.q - 1) if self.q > 1 else Season(self.year - 1, 4)

    @property
    def label(self) -> str:
        return f"{self.year} 年{PERIOD_LABEL[self.q]}"


def latest_ended(as_of: dt.date) -> Season:
    s = Season(as_of.year - 1911, (as_of.month - 1) // 3 + 1)
    while s.end >= as_of:
        s = s.prev()
    return s


class _Tables(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "table":
            self.tables.append([])
        elif tag == "tr" and self.tables:
            self.row = []
            self.tables[-1].append(self.row)
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)


def num(text: Any) -> float | None:
    if text is None:
        return None
    s = str(text).replace(",", "").strip()
    if s in ("", "-", "--", "N/A", "－"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _pick(header: list[str], test) -> int | None:
    return next((i for i, h in enumerate(header) if test(h)), None)


def parse_mops_summary(html: str, codes: set[str]) -> dict[str, dict[str, float | None]]:
    """財報彙總頁（依產業格式分成多個表）→ {代號: {eps, ni_parent, ocf}}。"""
    p = _Tables()
    p.feed(html)
    out: dict[str, dict[str, float | None]] = {}
    for table in p.tables:
        header = next((r for r in table if r and r[0] == "公司代號"), None)
        if not header:
            continue
        cols = {
            "eps": _pick(header, lambda h: h.startswith("基本每股盈餘")),
            "ni_parent": _pick(header, lambda h: h.startswith("淨利") and "歸屬於母公司業主" in h),
            "net_income": _pick(header, lambda h: h.startswith("本期淨利")),
            "ocf": _pick(header, lambda h: h.startswith("營業活動之淨現金流")),
        }
        for row in table:
            if not row or row[0] not in codes or len(row) < len(header):
                continue
            merged = out.setdefault(row[0], {k: None for k in cols})
            for k, i in cols.items():
                if i is not None and (v := num(row[i])) is not None:
                    merged[k] = v
    return out


@dataclass
class Table:
    key: str
    record: SourceRecord
    rows: dict[str, dict[str, Any]]


@dataclass
class Fundamentals:
    code: str
    market: str
    season: Season | None = None
    eps_ytd: float | None = None
    eps_ytd_prev: float | None = None  # 去年同期
    eps_ttm: float | None = None  # 近四季
    ni_ytd: float | None = None  # 歸屬母公司淨利（千元）
    ni_ttm: float | None = None
    ocf_ytd: float | None = None  # 營業活動淨現金流（千元）
    rev_month: str | None = None  # 11509
    rev_yoy: float | None = None
    rev_cum_yoy: float | None = None
    rev_mom: float | None = None  # 較上月增減 %
    rev_amount: float | None = None  # 當月營收（千元）
    rev_prev_month: str | None = None  # 前一個月，看趨勢用
    rev_prev_yoy: float | None = None
    rev_prev_mom: float | None = None
    pe: float | None = None
    pb: float | None = None
    dividend_yield: float | None = None
    valuation_date: dt.date | None = None
    shares: float | None = None
    market_cap: float | None = None  # 元
    market_cap_rank: int | None = None
    records: dict[str, SourceRecord] = field(default_factory=dict)  # 用途 → 來源

    @property
    def eps_growth(self) -> float | None:
        if self.eps_ytd is None or self.eps_ytd_prev is None or self.eps_ytd_prev <= 0:
            return None  # 去年同期虧損時成長率沒有意義
        return round((self.eps_ytd - self.eps_ytd_prev) / abs(self.eps_ytd_prev) * 100, 2)

    @property
    def roe(self) -> float | None:
        """近四季 ROE ≈ 股價淨值比 ÷ 本益比。"""
        if not self.pe or not self.pb or self.pe <= 0:
            return None
        return round(self.pb / self.pe * 100, 2)

    @property
    def cash_conversion(self) -> float | None:
        if self.ocf_ytd is None or not self.ni_ytd or self.ni_ytd <= 0:
            return None
        return round(self.ocf_ytd / self.ni_ytd, 2)

    @property
    def profitable_ttm(self) -> bool | None:
        if self.ni_ttm is not None:
            return self.ni_ttm > 0
        if self.eps_ttm is not None:
            return self.eps_ttm > 0
        return None

    def add_fact(self, ctx: Any, name: str) -> str | None:
        """一檔一筆財報事實（只寫可核對的數字）；沒有任何數字時回傳 None。"""
        parts: list[str] = []
        values: dict[str, float] = {}
        srcs: list[str] = []

        def cite(*roles: str) -> None:
            for r in roles:
                if r in self.records:
                    srcs.append(ctx.registry.add(self.records[r]))

        if self.season and self.eps_ytd is not None:
            text = f"{self.season.label}基本每股盈餘 {self.eps_ytd:g} 元"
            values["eps_ytd"] = self.eps_ytd
            cite("income")
            if self.eps_ytd_prev is not None:
                text += f"（去年同期 {self.eps_ytd_prev:g} 元）"
                values["eps_ytd_prev"] = self.eps_ytd_prev
                cite("income_prev")
            if self.eps_ttm is not None:
                text += f"，近四季合計 {self.eps_ttm:g} 元"
                values["eps_ttm"] = self.eps_ttm
                cite("income_prev", "income_q4")
            parts.append(text)
        if self.ni_ytd is not None:
            text = f"歸屬母公司淨利 {self.ni_ytd / 1e5:,.2f} 億元"
            values["ni_ytd_100m"] = round(self.ni_ytd / 1e5, 2)
            if self.ocf_ytd is not None:
                text += f"、營業活動淨現金流 {self.ocf_ytd / 1e5:,.2f} 億元"
                values["ocf_ytd_100m"] = round(self.ocf_ytd / 1e5, 2)
                cite("cashflow")
            parts.append(text)
        if self.rev_month and self.rev_yoy is not None:
            month = int(self.rev_month[-2:])
            text = f"{month} 月營收"
            if self.rev_amount is not None:
                text += f" {self.rev_amount / 1e5:,.2f} 億元、"
                values["rev_100m"] = round(self.rev_amount / 1e5, 2)
            if self.rev_mom is not None:
                text += f"較上月 {self.rev_mom:+.2f}%、"
                values["rev_mom_pct"] = round(self.rev_mom, 2)
            text += f"年增 {self.rev_yoy:+.2f}%"
            values["rev_yoy_pct"] = round(self.rev_yoy, 2)
            if self.rev_cum_yoy is not None:
                text += f"、累計年增 {self.rev_cum_yoy:+.2f}%"
                values["rev_cum_yoy_pct"] = round(self.rev_cum_yoy, 2)
            cite("revenue")
            if self.rev_prev_month and self.rev_prev_yoy is not None:
                text += f"（前一月 {int(self.rev_prev_month[-2:])} 月"
                if self.rev_prev_mom is not None:
                    text += f"較上月 {self.rev_prev_mom:+.2f}%、"
                    values["rev_prev_mom_pct"] = round(self.rev_prev_mom, 2)
                text += f"年增 {self.rev_prev_yoy:+.2f}%）"
                values["rev_prev_yoy_pct"] = round(self.rev_prev_yoy, 2)
                cite("revenue_prev")
            parts.append(text)
        if self.valuation_date and (self.pe or self.pb):
            text = f"{self.valuation_date:%m/%d} "
            text += f"本益比 {self.pe:g} 倍" if self.pe else "本益比無（近四季虧損）"
            if self.pb:
                text += f"、股價淨值比 {self.pb:g} 倍"
                values["pb"] = self.pb
            if self.pe:
                values["pe"] = self.pe
            parts.append(text)
            cite("valuation")
        if not parts:
            return None
        return ctx.facts.add(list(dict.fromkeys(srcs)), f"{self.code} {name} " + "；".join(parts), values,
                             "fundamentals", self.code)


class FundamentalsLoader:
    def __init__(self, client: HttpClient, cache_dir: Path, markets: dict[str, str], as_of: dt.date):
        self.client = client
        self.dir = cache_dir / "fundamentals"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.markets = markets
        self.as_of = as_of
        self.notes: list[str] = []

    def _codes(self, market: str) -> set[str]:
        return {c for c, m in self.markets.items() if m == market}

    def season_table(self, kind: str, market: str, season: Season) -> Table | None:
        tag = "" if season.final(self.as_of) else self.as_of.isoformat()
        url = MOPS_URL.format(page=PAGES[kind], typek=TYPEK[market], year=season.year, season=season.q)
        parsed = self.dir / f"{kind}_{market}_{season}{'_' + tag if tag else ''}.json"
        try:
            fetched = self.client.get_text(url, tag=tag)
        except FetchError as exc:
            self.notes.append(f"{MARKET_LABEL[market]} {season} {kind} 抓取失敗：{exc}")
            return None
        cached = json.loads(parsed.read_text(encoding="utf-8")) if parsed.exists() else None
        if cached and cached.get("parser") == PARSER_VERSION and cached.get("fetched_at") == fetched.fetched_at:
            rows = cached["rows"]
        else:
            rows = parse_mops_summary(fetched.data, self._codes(market))
            parsed.write_text(json.dumps({"parser": PARSER_VERSION, "fetched_at": fetched.fetched_at, "rows": rows},
                                         ensure_ascii=False), encoding="utf-8")
        title = {"income": "綜合損益表", "cashflow": "現金流量表"}[kind]
        key = f"mops:{kind}:{market}:{season}"
        record = SourceRecord(
            key=key, authority="mops", url=url,
            content_as_of=iso_tpe(dt.datetime.combine(season.end, dt.time(0, 0), TPE)),
            name=f"公開資訊觀測站 {MARKET_LABEL[market]}公司 {season.year} 年第 {season.q} 季{title}彙總表（累計）",
            fetched_at=fetched.fetched_at, snapshot=str(fetched.snapshot),
        )
        return Table(key, record, rows)

    def _openapi(self, kind: str, market: str) -> tuple[list[dict[str, Any]], Any] | None:
        url = OPENAPI[(kind, market)]
        try:
            fetched = self.client.get_json(url, tag=self.as_of.isoformat())
        except FetchError as exc:
            self.notes.append(f"{MARKET_LABEL[market]} {kind} 抓取失敗：{exc}")
            return None
        return (fetched.data if isinstance(fetched.data, list) else []), fetched

    def load(self, closes: dict[str, float]) -> dict[str, Fundamentals]:
        out: dict[str, Fundamentals] = {}
        latest = latest_ended(self.as_of)
        for market in ("twse", "tpex"):
            codes = self._codes(market)
            if not codes:
                continue
            for c in codes:
                out[c] = Fundamentals(c, market)
            self._load_statements(market, codes, latest, out)
            self._load_revenue(market, codes, out)
            self._load_valuation(market, codes, out)
            self._load_profile(market, codes, out)
        caps = {c: f.shares * closes[c] for c, f in out.items() if f.shares and closes.get(c)}
        for rank, c in enumerate(sorted(caps, key=lambda c: -caps[c]), 1):
            out[c].market_cap, out[c].market_cap_rank = caps[c], rank
        return out

    def _load_statements(self, market: str, codes: set[str], latest: Season, out: dict[str, Fundamentals]) -> None:
        tables: dict[tuple[str, Season], Table | None] = {}

        def get(kind: str, s: Season) -> Table | None:
            if (kind, s) not in tables:
                tables[(kind, s)] = self.season_table(kind, market, s)
            return tables[(kind, s)]

        for c in sorted(codes):
            season = next((s for s in (latest, latest.prev()) if (t := get("income", s)) and c in t.rows), None)
            if season is None:
                continue
            f, inc = out[c], get("income", season)
            row = inc.rows[c]
            f.season, f.eps_ytd = season, row["eps"]
            f.ni_ytd = row["ni_parent"] if row["ni_parent"] is not None else row["net_income"]
            f.records["income"] = inc.record
            prev = get("income", Season(season.year - 1, season.q))
            if prev and c in prev.rows:
                f.eps_ytd_prev = prev.rows[c]["eps"]
                f.records["income_prev"] = prev.record
                prev_ni = prev.rows[c]["ni_parent"] if prev.rows[c]["ni_parent"] is not None else prev.rows[c]["net_income"]
                if season.q == 4:
                    f.eps_ttm, f.ni_ttm = f.eps_ytd, f.ni_ytd
                else:
                    q4 = get("income", Season(season.year - 1, 4))
                    if q4 and c in q4.rows:
                        f.records["income_q4"] = q4.record
                        r4 = q4.rows[c]
                        ni4 = r4["ni_parent"] if r4["ni_parent"] is not None else r4["net_income"]
                        if None not in (f.eps_ytd, r4["eps"], f.eps_ytd_prev):
                            f.eps_ttm = round(f.eps_ytd + r4["eps"] - f.eps_ytd_prev, 2)
                        if None not in (f.ni_ytd, ni4, prev_ni):
                            f.ni_ttm = f.ni_ytd + ni4 - prev_ni
            cf = get("cashflow", season)
            if cf and c in cf.rows:
                f.ocf_ytd = cf.rows[c]["ocf"]
                f.records["cashflow"] = cf.record
        found = sum(1 for c in codes if out[c].season)
        if found < len(codes):
            self.notes.append(f"{MARKET_LABEL[market]} {len(codes) - found} 檔找不到最近兩季財報")

    def revenue_page(self, market: str, year: int, month: int, foreign: bool) -> Table | None:
        """公開資訊觀測站月營收彙總（本國／外國企業各一頁）；次月 10 日前公告，過了 12 日視為定案。"""
        published = dt.date(year + 1911 + (month == 12), month % 12 + 1, 12)  # 12 日後視為定案
        tag = "" if self.as_of > published else self.as_of.isoformat()
        url = REVENUE_URL.format(typek=TYPEK[market], year=year, month=month, foreign=int(foreign))
        try:
            fetched = self.client.get_text(url, tag=tag, encoding="cp950")
        except FetchError:
            return None
        p = _Tables()
        p.feed(fetched.data)
        rows: dict[str, dict[str, Any]] = {}
        for table in p.tables:
            for row in table:
                if len(row) >= 10 and re.fullmatch(r"\d{4,6}[A-Z]?", row[0]):
                    # 欄位：代號、名稱、當月營收、上月營收、去年當月、上月比較增減%、去年同月增減%、當月累計、去年累計、前期比較增減%
                    rows[row[0]] = {"yoy": num(row[6]), "cum_yoy": num(row[9]), "mom": num(row[5]), "amount": num(row[2])}
        if not rows:
            return None
        y, m = year + 1911, month
        record = SourceRecord(
            key=f"mops:revenue:{market}:{year}{month:02d}:{int(foreign)}", authority="mops", url=url,
            content_as_of=iso_tpe(dt.datetime.combine(dt.date(y, m, calendar.monthrange(y, m)[1]), dt.time(0, 0), TPE)),
            name=f"公開資訊觀測站 {MARKET_LABEL[market]}{'外國' if foreign else ''}公司 {year} 年 {month} 月營業收入統計表",
            fetched_at=fetched.fetched_at, snapshot=str(fetched.snapshot),
        )
        return Table(record.key, record, rows)

    def _load_revenue(self, market: str, codes: set[str], out: dict[str, Fundamentals]) -> None:
        # 法定期限（次月 10 日）過後才用該月：避免公告期間只有部分公司的半成品，演練時也不會前視
        last = self.as_of.replace(day=1) - dt.timedelta(days=1)
        if self.as_of.day <= 10:
            last = last.replace(day=1) - dt.timedelta(days=1)
        prev = last.replace(day=1) - dt.timedelta(days=1)
        months = [(last.year - 1911, last.month), (prev.year - 1911, prev.month)]
        for foreign in (False, True):
            todo = {c for c in codes if out[c].rev_month is None}
            got: set[str] = set()
            for year, month in months:
                if not todo:
                    break
                table = self.revenue_page(market, year, month, foreign)
                if table is None:
                    continue
                for c in [c for c in todo if c in table.rows]:
                    f, row = out[c], table.rows[c]
                    f.rev_month = f"{year}{month:02d}"
                    f.rev_yoy, f.rev_cum_yoy = row["yoy"], row["cum_yoy"]
                    f.rev_mom, f.rev_amount = row.get("mom"), row.get("amount")
                    f.records["revenue"] = table.record
                    todo.discard(c)
                    got.add(c)
            # 前一個月的年增與月增減：看營收動能是延續還是轉弱
            pages: dict[tuple[int, int], Table | None] = {}
            for c in sorted(got):
                f = out[c]
                y, m = int(f.rev_month[:-2]), int(f.rev_month[-2:])
                py, pm = (y, m - 1) if m > 1 else (y - 1, 12)
                if (py, pm) not in pages:
                    pages[(py, pm)] = self.revenue_page(market, py, pm, foreign)
                table = pages[(py, pm)]
                if table is not None and c in table.rows:
                    f.rev_prev_month = f"{py}{pm:02d}"
                    f.rev_prev_yoy, f.rev_prev_mom = table.rows[c]["yoy"], table.rows[c].get("mom")
                    f.records["revenue_prev"] = table.record

    def _load_valuation(self, market: str, codes: set[str], out: dict[str, Fundamentals]) -> None:
        """指定日期（資料截止日）的本益比、殖利率、股價淨值比；按日期查詢，不會用到未來資料。"""
        day = self.as_of
        url = VALUATION_URL[market].format(ymd=f"{day:%Y%m%d}", y=day.year, m=f"{day:%m}", d=f"{day:%d}")
        try:
            fetched = self.client.get_json(url)
        except FetchError as exc:
            self.notes.append(f"{MARKET_LABEL[market]}本益比抓取失敗：{exc}")
            return
        data = fetched.data if isinstance(fetched.data, dict) else {}
        table = (data.get("tables") or [{}])[0] if market == "tpex" else data
        fields, rows = table.get("fields") or [], table.get("data") or []
        idx = {k: next((i for i, f in enumerate(fields) if f.startswith(k)), None)
               for k in ("證券代號" if market == "twse" else "股票代號", "本益比", "股價淨值比", "殖利率")}
        if not rows or None in idx.values():
            self.client.forget(url)  # 尚未公布：下次重抓
            self.notes.append(f"{MARKET_LABEL[market]} {day} 的本益比資料尚未公布")
            return
        code_i, pe_i, pb_i, dy_i = idx.values()
        label = "證交所上市個股本益比、殖利率及股價淨值比" if market == "twse" else "櫃買中心上櫃個股本益比、殖利率及股價淨值比"
        record = SourceRecord(
            key=f"{market}:valuation:{day.isoformat()}", authority=market, url=url,
            content_as_of=iso_tpe(dt.datetime.combine(day, CLOSE_TIME, TPE)), name=f"{label} {day.isoformat()}",
            fetched_at=fetched.fetched_at, snapshot=str(fetched.snapshot),
        )
        for r in rows:
            c = str(r[code_i]).strip()
            if c not in codes:
                continue
            f = out[c]
            pe = num(r[pe_i])
            f.pe = pe if pe and pe > 0 else None
            f.pb = num(r[pb_i]) or None
            f.dividend_yield = num(r[dy_i])
            f.valuation_date = day
            f.records["valuation"] = record

    def _load_profile(self, market: str, codes: set[str], out: dict[str, Fundamentals]) -> None:
        got = self._openapi("profile", market)
        if not got:
            return
        rows, _ = got
        keys = ("公司代號", "實收資本額", "普通股每股面額") if market == "twse" else \
               ("SecuritiesCompanyCode", "Paidin.Capital.NTDollars", "ParValueOfCommonStock")
        for r in rows:
            c = r.get(keys[0])
            if c not in codes:
                continue
            capital = num(r.get(keys[1]))
            par_match = re.search(r"([\d.]+)", str(r.get(keys[2]) or ""))
            par = float(par_match.group(1)) if par_match and float(par_match.group(1)) > 0 else 10.0
            if capital:
                out[c].shares = capital / par
