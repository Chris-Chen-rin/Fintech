from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from agent.settings import EXAMPLE_SETTINGS_PATH, SettingsError, code_version, get_secret, load_settings
from agent.trading_calendar import TPE, TradingCalendar, iso_tpe, to_tpe


def write_settings(tmp_path: Path, **overrides) -> Path:
    raw = yaml.safe_load(EXAMPLE_SETTINGS_PATH.read_text(encoding="utf-8"))
    for key, value in overrides.items():
        section, _, field = key.partition("__")
        if field:
            raw[section][field] = value
        else:
            raw[section] = value
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


# ---------------------------------------------------------------- 設定

def test_example_settings_load_with_warnings(tmp_path):
    s = load_settings(write_settings(tmp_path, paths={"data_root": str(tmp_path)}))
    assert s.team_id == "TEAM_000"
    assert s.paths.root == tmp_path
    assert any("TEAM_000" in w for w in s.warnings)
    assert any("theme.tickers" in w for w in s.warnings)
    assert s.risk.weight_cap("2330") == 0.23 and s.risk.weight_cap("2317") == 0.09


def test_missing_settings_file(tmp_path):
    with pytest.raises(SettingsError, match="找不到設定檔"):
        load_settings(tmp_path / "nope.yaml")


@pytest.mark.parametrize("overrides, message", [
    ({"team_id": "TEAM 1"}, "team_id"),
    ({"theme__tickers": ["2330", "9999"]}, "9999"),
    ({"risk__stock_max_weight": 0.11}, "stock_max_weight"),
    ({"risk__tsmc_max_weight": 0.26}, "tsmc_max_weight"),
    ({"risk__max_holdings": 31}, "持股檔數"),
    ({"risk__cash_target_range": [0.1, 0.3]}, "cash_target_range"),
    ({"risk__first_day_cash_target": 0.2}, "first_day_cash_target"),
    ({"llm__provider": "google"}, "llm.model"),
    ({"holidays": ["2026-13-01"]}, "holidays"),
])
def test_invalid_settings(tmp_path, overrides, message):
    with pytest.raises(SettingsError, match=message):
        load_settings(write_settings(tmp_path, **overrides))


def test_data_root_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DATA_ROOT", str(tmp_path / "drive"))
    s = load_settings(write_settings(tmp_path))
    assert s.paths.root == tmp_path / "drive"
    s.paths.ensure()
    assert s.paths.inbox.is_dir() and s.paths.archive_for(dt.date(2026, 10, 26)).name == "2026-10-26"


def test_holidays_parsed(tmp_path):
    s = load_settings(write_settings(tmp_path, holidays=["2026-10-26"]))
    assert dt.date(2026, 10, 26) in s.holidays


def test_get_secret_from_env(monkeypatch):
    monkeypatch.setenv("UNIT_TEST_SECRET", "abc")
    assert get_secret("UNIT_TEST_SECRET") == "abc"
    assert get_secret("") is None


def test_code_version_format():
    import re
    assert re.fullmatch(r"[A-Za-z0-9_.:/\-]{1,64}", code_version())


# ---------------------------------------------------------------- 交易日曆

def at(day: str, hm: str) -> dt.datetime:
    return dt.datetime.fromisoformat(f"{day}T{hm}:00").replace(tzinfo=TPE)


def test_competition_days_without_holiday_is_25():
    assert len(TradingCalendar().competition_days()) == 25
    assert len(TradingCalendar({dt.date(2026, 11, 2)}).competition_days()) == 24


@pytest.mark.parametrize("moment, trade_date, status, session", [
    (at("2026-10-25", "20:00"), "2026-10-26", "open", "evening"),      # 首日：週日晚上
    (at("2026-10-25", "18:00"), "2026-10-26", "not_open", "evening"),
    (at("2026-10-26", "07:00"), "2026-10-26", "open", "morning"),
    (at("2026-10-26", "08:55"), "2026-10-26", "open", "morning"),
    (at("2026-10-26", "09:00"), "2026-10-27", "not_open", "evening"),
    (at("2026-10-26", "19:30"), "2026-10-27", "open", "evening"),
    (at("2026-10-30", "20:00"), "2026-11-02", "not_open", "evening"),  # 週五晚上：週一的時段週日 19:30 才開
    (at("2026-11-01", "21:00"), "2026-11-02", "open", "evening"),
])
def test_target_window(moment, trade_date, status, session):
    w = TradingCalendar().target_window(moment)
    assert w.trade_date.isoformat() == trade_date
    assert w.status(moment) == status
    assert w.session(moment) == session


def test_window_skips_holiday_and_prev_trading_day():
    cal = TradingCalendar({dt.date(2026, 11, 2)})
    w = cal.target_window(at("2026-10-30", "20:00"))
    assert w.trade_date == dt.date(2026, 11, 3)
    assert cal.window(dt.date(2026, 11, 3)).prev_trading_date == dt.date(2026, 10, 30)


def test_utc_moment_converted_to_taipei():
    utc_moment = dt.datetime(2026, 10, 25, 23, 0, tzinfo=dt.timezone.utc)  # 台北 10/26 07:00
    w = TradingCalendar().target_window(utc_moment)
    assert w.trade_date == dt.date(2026, 10, 26) and w.session(utc_moment) == "morning"


def test_iso_format_matches_dplan_pattern():
    assert iso_tpe(dt.datetime(2026, 10, 27, 7, 52, 36, 123456)) == "2026-10-27T07:52:36+08:00"
    assert to_tpe(dt.datetime(2026, 10, 26, 0, 0, tzinfo=dt.timezone.utc)).hour == 8
