"""M4 來源登錄：每份實際使用的資料 → D-Plan sources 的一筆（組裝時才給 S 編號）。"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .trading_calendar import TPE, iso_tpe
from .tools.market_data import CLOSE_TIME, DayQuotes

AUTHORITIES = {"twse", "tpex", "taifex", "mops", "fininst", "media", "vendor", "other"}


@dataclass(frozen=True)
class SourceRecord:
    key: str
    authority: str
    url: str
    content_as_of: str
    name: str | None = None
    fetched_at: str | None = None
    archive_url: str | None = None
    published_at: str | None = None
    snapshot: str | None = None

    def to_dplan(self, source_id: str) -> dict[str, str]:
        out = {"source_id": source_id, "authority": self.authority}
        if self.name:
            out["name"] = self.name[:300]
        out["url"] = self.url
        if self.archive_url:
            out["archive_url"] = self.archive_url
        if self.published_at:
            out["published_at"] = self.published_at
        out["content_as_of"] = self.content_as_of
        if self.fetched_at:
            out["fetched_at"] = self.fetched_at
        return out


class SourceRegistry:
    def __init__(self) -> None:
        self._records: dict[str, SourceRecord] = {}

    def add(self, record: SourceRecord) -> str:
        if record.authority not in AUTHORITIES:
            raise ValueError(f"authority {record.authority!r} 不在規定清單內")
        existing = self._records.get(record.key)
        if existing and existing != record:
            raise ValueError(f"來源代號 {record.key} 重複登錄且內容不同")
        self._records[record.key] = record
        return record.key

    def add_day_quotes(self, dq: DayQuotes) -> str:
        label = "證交所上市每日收盤行情（含加權指數）" if dq.market == "twse" else "櫃買中心上櫃股票每日收盤行情"
        return self.add(SourceRecord(
            key=f"{dq.market}:{dq.date.isoformat()}",
            authority=dq.market,
            url=dq.url,
            content_as_of=iso_tpe(dt.datetime.combine(dq.date, CLOSE_TIME, TPE)),
            name=f"{label} {dq.date.isoformat()}",
            fetched_at=dq.fetched_at,
            snapshot=dq.snapshot,
        ))

    def get(self, key: str) -> SourceRecord:
        return self._records[key]

    def __contains__(self, key: str) -> bool:
        return key in self._records

    def all(self) -> list[SourceRecord]:
        return list(self._records.values())
