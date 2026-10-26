"""平行回測共用快取：休市日不重寫、寫入整檔替換。"""
from __future__ import annotations

import datetime as dt
import json

from agent.tools.http import HttpClient, atomic_write
from agent.tools.market_data import PriceStore, load_history


def test_atomic_write_replaces_whole_file(tmp_path):
    p = tmp_path / "a.json"
    atomic_write(p, json.dumps({"v": 1}))
    atomic_write(p, b'{"v": 2}')
    assert json.loads(p.read_text(encoding="utf-8")) == {"v": 2}
    assert not list(tmp_path.glob("*.tmp"))


def test_closed_day_is_not_rewritten(tmp_path):
    store = PriceStore(HttpClient(tmp_path, offline=True), tmp_path, {}, {})
    day = dt.date(2026, 2, 17)
    store.mark_closed(day)
    paths = [store._path(m, day) for m in ("twse", "tpex")]
    stamps = [p.stat().st_mtime_ns for p in paths]
    store.mark_closed(day)
    assert [p.stat().st_mtime_ns for p in paths] == stamps
    assert store.day("twse", day) is None and store.day("tpex", day) is None


def test_load_history_skips_known_closed_day(tmp_path, monkeypatch):
    store = PriceStore(HttpClient(tmp_path, offline=True), tmp_path, {}, {})
    closed = dt.date(2026, 2, 17)
    store.mark_closed(closed)
    calls = []
    monkeypatch.setattr("agent.tools.market_data.atomic_write", lambda *a: calls.append(a))

    class Q:
        quotes, indices = {}, {}

    real_day = store.day
    store.day = lambda m, d: None if d == closed else (Q() if d < closed + dt.timedelta(days=3) else real_day(m, d))
    load_history(store, dt.date(2026, 2, 18), 3, lambda d: d.weekday() < 5, max_lookback=10)
    assert calls == []
