"""公開資訊觀測站「每日重大訊息」：依日期抓全體公司公告，只留 150 檔標的。

一天一頁（ajax_t05st02，含前一日 17:30 以後的訊息），每列有公司代號、發言時間、主旨與說明全文。
只保留發言時間早於執行時點的訊息，演練過去日期時不會看到未來資訊。
"""
from __future__ import annotations

import datetime as dt
import html
import re
from dataclasses import dataclass

from ..sources import SourceRecord
from ..trading_calendar import TPE, iso_tpe
from .http import FetchError, HttpClient

NEWS_URL = ("https://mopsov.twse.com.tw/mops/web/ajax_t05st02?encodeURIComponent=1&step=1&step00=0&firstin=1&off=1"
            "&TYPEK=all&year={roc}&month={m:02d}&day={d:02d}")
_FIELD = re.compile(r"name='h\d*?(\d)' value='(.*?)'>", re.S)  # 欄位名 h<列號><欄位 0–8>，只看最後一位


@dataclass
class Announcement:
    code: str
    name: str
    at: dt.datetime
    subject: str
    detail: str
    source_key: str

    @property
    def text(self) -> str:
        return f"{self.subject}\n{self.detail}"


def parse_news_page(page: str, source_key: str) -> list[Announcement]:
    out: list[Announcement] = []
    for form in page.split("<form ")[1:]:
        f = {k: html.unescape(v).replace("\r", "").strip() for k, v in _FIELD.findall(form)}
        if not f.get("1") or not f.get("2"):
            continue
        try:
            at = dt.datetime.strptime(f["2"] + f.get("3", "0").zfill(6), "%Y%m%d%H%M%S").replace(tzinfo=TPE)
        except ValueError:
            continue
        out.append(Announcement(f["1"], f.get("0", ""), at, re.sub(r"\s+", "", f.get("4", "")),
                                f.get("8", ""), source_key))
    return out


class AnnouncementLoader:
    def __init__(self, client: HttpClient, codes: set[str], moment: dt.datetime, days: int = 5):
        self.client = client
        self.codes = codes
        self.moment = moment
        self.days = days
        self.notes: list[str] = []
        self.records: dict[str, SourceRecord] = {}

    def load(self) -> list[Announcement]:
        seen: set[tuple] = set()
        out: list[Announcement] = []
        today = self.moment.date()
        for back in range(self.days):
            day = today - dt.timedelta(days=back)
            url = NEWS_URL.format(roc=day.year - 1911, m=day.month, d=day.day)
            tag = "" if day < today - dt.timedelta(days=1) else self.moment.strftime("%Y-%m-%dT%H")
            key = f"mops:news:{day.isoformat()}"
            try:
                fetched = self.client.get_text(url, tag=tag)
            except FetchError as exc:
                self.notes.append(f"{day} 重大訊息抓取失敗：{exc}")
                continue
            rows = [a for a in parse_news_page(fetched.data, key) if a.at <= self.moment]
            if not rows:
                continue
            self.records[key] = SourceRecord(
                key=key, authority="mops", url=url, content_as_of=iso_tpe(max(a.at for a in rows)),
                name=f"公開資訊觀測站 每日重大訊息 {day.isoformat()}（全體公司）",
                fetched_at=fetched.fetched_at, snapshot=str(fetched.snapshot))
            for a in rows:
                ident = (a.code, a.at, a.subject)
                if a.code in self.codes and ident not in seen:
                    seen.add(ident)
                    out.append(a)
        return sorted(out, key=lambda a: a.at, reverse=True)
