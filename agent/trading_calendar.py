from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterable

from dplan_checker import rules as R

TPE = R.TPE


def now_tpe() -> dt.datetime:
    return dt.datetime.now(TPE)


def to_tpe(moment: dt.datetime) -> dt.datetime:
    """沒有時區的時間視為台北時間；有時區的轉成台北時間（Colab 主機是 UTC）。"""
    return moment.replace(tzinfo=TPE) if moment.tzinfo is None else moment.astimezone(TPE)


def iso_tpe(moment: dt.datetime) -> str:
    """D-Plan 規定的時間格式：2026-10-27T07:52:36+08:00（不含小數秒）。"""
    return to_tpe(moment).replace(microsecond=0).isoformat()


class TradingCalendar:
    def __init__(self, holidays: Iterable[dt.date] = ()):
        self.holidays = frozenset(holidays)

    def is_trading_day(self, day: dt.date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def next_trading_day(self, day: dt.date, include_self: bool = False) -> dt.date:
        if not include_self:
            day += dt.timedelta(days=1)
        while not self.is_trading_day(day):
            day += dt.timedelta(days=1)
        return day

    def prev_trading_day(self, day: dt.date) -> dt.date:
        day -= dt.timedelta(days=1)
        while not self.is_trading_day(day):
            day -= dt.timedelta(days=1)
        return day

    def competition_days(self) -> list[dt.date]:
        days, day = [], R.COMPETITION_START
        while day <= R.COMPETITION_END:
            if self.is_trading_day(day):
                days.append(day)
            day += dt.timedelta(days=1)
        return days

    def window(self, trade_date: dt.date) -> SubmissionWindow:
        opens = dt.datetime.combine(trade_date - dt.timedelta(days=1), R.SUBMIT_OPEN_TIME, TPE)
        deadline = dt.datetime.combine(trade_date, R.SUBMIT_DEADLINE_TIME, TPE)
        return SubmissionWindow(trade_date, self.prev_trading_day(trade_date), opens, deadline)

    def target_window(self, moment: dt.datetime | None = None) -> SubmissionWindow:
        """現在要準備哪一個交易日的 D-Plan：今天 08:55 前是今天，之後是下一個交易日。"""
        moment = to_tpe(moment or now_tpe())
        today = moment.date()
        if self.is_trading_day(today) and moment <= dt.datetime.combine(today, R.SUBMIT_DEADLINE_TIME, TPE):
            return self.window(today)
        return self.window(self.next_trading_day(today))


@dataclass(frozen=True)
class SubmissionWindow:
    trade_date: dt.date
    prev_trading_date: dt.date
    opens_at: dt.datetime
    deadline: dt.datetime

    def status(self, moment: dt.datetime | None = None) -> str:
        moment = to_tpe(moment or now_tpe())
        if moment < self.opens_at:
            return "not_open"
        if moment <= self.deadline:
            return "open"
        return "closed"

    def session(self, moment: dt.datetime | None = None) -> str:
        """evening：前一晚的基準版；morning：交易日早上的更新版。"""
        moment = to_tpe(moment or now_tpe())
        return "morning" if moment.date() == self.trade_date else "evening"

    @property
    def in_competition(self) -> bool:
        return R.COMPETITION_START <= self.trade_date <= R.COMPETITION_END

    def describe(self, moment: dt.datetime | None = None) -> str:
        label = {"not_open": "尚未開放", "open": "開放中", "closed": "已截止"}[self.status(moment)]
        return (f"交易日 {self.trade_date}（前一交易日 {self.prev_trading_date}）；"
                f"繳交時段 {self.opens_at:%m/%d %H:%M} ～ {self.deadline:%m/%d %H:%M}，目前{label}")
