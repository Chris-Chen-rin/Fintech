from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path

import pytest

from dplan_checker import CheckOptions, check_dplan
from dplan_checker.rules import TPE

DOCS = Path(__file__).resolve().parents[1] / "docs"
TEAM = "TEAM_099"
TRADE_DATE = "2026-10-28"
NOW = dt.datetime(2026, 10, 28, 7, 30, tzinfo=TPE)

OTHERS = ["2317", "2454", "2308", "2382", "2412", "2881", "2882", "2303", "3711", "2891",
          "1301", "1303", "2002", "2886", "2884", "3008", "2357", "2603", "1216", "2912", "3045"]
NEW_BUY = "2885"


def base_datapack() -> dict:
    holdings = [{"code": "2330", "shares": 200_000, "receivable_shares": 0, "close_price": 1000.0, "tradable_weight": 0.2}]
    holdings += [{"code": c, "shares": 333_000, "receivable_shares": 0, "close_price": 100.0, "tradable_weight": 0.0333}
                 for c in OTHERS]
    return {
        "schema_version": "4.3",
        "doc_type": "DataPack",
        "team_id": TEAM,
        "trade_date": TRADE_DATE,
        "as_of_date": "2026-10-27",
        "generated_at": "2026-10-27T18:58:18+08:00",
        "missing": [],
        "prev_day_orders": {"outcome": "executed", "void_reasons": [], "excluded_codes": []},
        "account": {"cash": 100_700_000.0, "cash_ratio_tradable": 0.1007, "nav_tradable": 1_000_000_000.0,
                    "aum": 1_000_000_000.0, "receivable_cash": 0.0, "holdings_count": 22},
        "holdings": holdings,
        "compliance": {"violations": 0, "notices": 0, "incidents": []},
    }


def base_dplan() -> dict:
    return {
        "schema_version": "4.3",
        "doc_type": "D-Plan",
        "team_id": TEAM,
        "trade_date": TRADE_DATE,
        "sources": [
            {"source_id": "S1", "authority": "twse", "url": "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
             "content_as_of": "2026-10-27T18:30:00+08:00", "fetched_at": "2026-10-28T05:04:12+08:00"},
            {"source_id": "S2", "authority": "media", "url": "https://www.reuters.com/markets/us/fed-2026-10-27/",
             "archive_url": "https://web.archive.org/web/20261027/https://www.reuters.com/markets/us/fed-2026-10-27/",
             "published_at": "2026-10-28T03:41:00+08:00", "content_as_of": "2026-10-28T03:41:00+08:00",
             "fetched_at": "2026-10-28T05:10:00+08:00"},
        ],
        "observations": [
            {"obs_id": "O1", "source_ref": ["S1"], "statement": "TAIEX 收 23,412.5（-0.62%），金融類股指數 +0.4%",
             "values": {"TAIEX": 23412.5, "TAIEX_chg_pct": -0.62, "fin_sector_chg_pct": 0.4}},
            {"obs_id": "O2", "source_ref": ["S2"], "statement": "市場隱含 12 月降息一碼機率 71%",
             "values": {"dec_cut_prob_pct": 71}},
        ],
        "market_view": {
            "basis_refs": ["O1", "O2"],
            "logic": "大盤小跌但金融逆勢（O1），降息機率升高（O2），判定中性震盪，換股不改總曝險",
            "regime": "neutral", "stance": "neutral",
            "posture": {"net_exposure_intent": "hold", "target_cash_pct_range": [0.03, 0.15]},
            "counter_evidence": None,
        },
        "inferences": [
            {"inf_id": "I1", "premise_refs": ["O1"], "logic": "台積電 2330 權重 20% 偏高，大盤走弱下調降至 18% 以控制集中度",
             "counter_evidence": None},
            {"inf_id": "I2", "premise_refs": ["O2"], "logic": "降息機率 71% 有利金融股，新建 2885 元大金 2% 部位",
             "counter_evidence": None},
            {"inf_id": "I3", "premise_refs": ["O1"],
             "logic": "其餘持股 " + "、".join(OTHERS) + " 無新事證，權重偏離在無交易帶內，維持不動",
             "counter_evidence": None},
        ],
        "decisions": [
            {"decision_id": "D1", "ticker": "2330", "action": "TRIM", "target_weight": 0.18, "inference_refs": ["I1"]},
            {"decision_id": "D2", "ticker": NEW_BUY, "action": "BUY", "target_weight": 0.02, "inference_refs": ["I2"]},
        ],
        "no_trade_decisions": [{"ticker": c, "reason_refs": ["I3"]} for c in OTHERS],
        "orders": [
            {"ticker": "2330", "side": "SELL", "shares": 20_000, "decision_ref": "D1"},
            {"ticker": NEW_BUY, "side": "BUY", "shares": 400_000, "decision_ref": "D2"},
        ],
        "agent_metadata": {
            "model_provider": "google", "model_version": "gemini-2.5-pro",
            "run_started_at": "2026-10-28T06:00:00+08:00", "run_completed_at": "2026-10-28T07:00:00+08:00",
            "code_version": "git:0f1e2d3",
        },
    }


def run(tmp_path: Path, dplan: dict | str | None = None, datapack: dict | None = None, *,
        filename: str | None = None, prices: dict | None = None, use_datapack: bool = True, **opts):
    dplan = base_dplan() if dplan is None else dplan
    if isinstance(dplan, dict):
        team, date = dplan.get("team_id", TEAM), dplan.get("trade_date", TRADE_DATE)
        text = json.dumps(dplan, ensure_ascii=False)
    else:
        team, date, text = TEAM, TRADE_DATE, dplan
    path = tmp_path / (filename or f"D-Plan_{team}_{date}.json")
    path.write_text(text, encoding="utf-8")

    dp_path = None
    if use_datapack:
        dp_path = tmp_path / "datapack.json"
        dp_path.write_text(json.dumps(datapack or base_datapack()), encoding="utf-8")
    price_path = tmp_path / "prices.json"
    price_path.write_text(json.dumps(prices if prices is not None else {NEW_BUY: 50.0, "2892": 30.0}), encoding="utf-8")
    opts.setdefault("team_id", TEAM)
    opts.setdefault("now", NOW)
    options = CheckOptions(datapack_path=dp_path, prices_path=price_path, **opts)
    return check_dplan(path, options)


def errors(report) -> set[str]:
    return report.codes("ERROR")


def warns(report) -> set[str]:
    return report.codes("WARN")


# ---------------------------------------------------------------- 基準

def test_baseline_is_clean(tmp_path):
    report = run(tmp_path)
    assert report.ok, report.format_text()
    assert not report.warnings, report.format_text()
    assert report.summary["交易後持股檔數"] == 23


# ---------------------------------------------------------------- 官方範例

def _example_datapack(team: str) -> dict:
    nav = 102_000_000.0
    holdings = [
        {"code": "2330", "shares": 17_000, "receivable_shares": 0, "close_price": 1480.0, "tradable_weight": 0.2467},
        {"code": "2317", "shares": 40_000, "receivable_shares": 0, "close_price": 200.0, "tradable_weight": 0.0784},
        {"code": "2412", "shares": 60_000, "receivable_shares": 0, "close_price": 120.0, "tradable_weight": 0.0706},
    ]
    equity = sum(h["shares"] * h["close_price"] for h in holdings)
    dp = base_datapack()
    dp.update(team_id=team, holdings=holdings)
    dp["account"] = {"cash": nav - equity, "cash_ratio_tradable": (nav - equity) / nav, "nav_tradable": nav,
                     "aum": nav, "receivable_cash": 0.0, "holdings_count": 3}
    return dp


def test_official_negative_example_catches_all_three_problems(tmp_path):
    dplan = json.loads((DOCS / "D-Plan_TEAM_043_2026-10-28.json").read_text(encoding="utf-8"))
    report = run(tmp_path, dplan, _example_datapack("TEAM_043"), prices={"2891": 42.5, "2454": 1250.0},
                 team_id="TEAM_043", now=dt.datetime(2026, 10, 28, 8, 50, tzinfo=TPE))
    errs = errors(report)
    assert "REF_BROKEN" in errs                 # 孤兒決策 D3 → I9
    assert "ORDER_DERIVATION_MISMATCH" in errs  # 2330 應賣 2000 股，寫 3000
    assert "POSTURE_NET_FLOW" in errs           # 宣告 reduce 卻淨買超


def test_official_positive_example_derivation_is_consistent(tmp_path):
    dplan = json.loads((DOCS / "D-Plan_TEAM_042_2026-10-28.json").read_text(encoding="utf-8"))
    report = run(tmp_path, dplan, _example_datapack("TEAM_042"), prices={"2891": 42.5}, team_id="TEAM_042")
    structural = {"SCHEMA", "REF_BROKEN", "COVERAGE_MISSING", "ORDER_DERIVATION_MISMATCH", "ORDER_MISSING",
                  "ORDER_UNEXPECTED", "OVERSELL", "POSTURE_NET_FLOW", "ACTION_DERIVATION_MISMATCH"}
    assert not structural & errors(report), report.format_text()


# ---------------------------------------------------------------- 檔案與 JSON

def test_bad_filename(tmp_path):
    assert "FILENAME_FORMAT" in errors(run(tmp_path, filename="D-Plan_TEAM_099_2026-10-28 (1).json"))


def test_filename_team_mismatch(tmp_path):
    assert "FILENAME_TEAM_MISMATCH" in errors(run(tmp_path, filename="D-Plan_TEAM_100_2026-10-28.json"))


def test_duplicate_json_key(tmp_path):
    text = json.dumps(base_dplan(), ensure_ascii=False).replace('"doc_type": "D-Plan"', '"doc_type": "D-Plan", "doc_type": "D-Plan"', 1)
    assert "JSON_DUPLICATE_KEY" in errors(run(tmp_path, text))


def test_nan_rejected(tmp_path):
    text = json.dumps(base_dplan(), ensure_ascii=False).replace('"dec_cut_prob_pct": 71', '"dec_cut_prob_pct": NaN')
    assert "JSON_INVALID" in errors(run(tmp_path, text))


def test_bom_warning(tmp_path):
    path = tmp_path / f"D-Plan_{TEAM}_{TRADE_DATE}.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(base_dplan(), ensure_ascii=False).encode("utf-8"))
    report = check_dplan(path, CheckOptions(team_id=TEAM, now=NOW))
    assert "JSON_BOM" in warns(report)


# ---------------------------------------------------------------- schema

def test_extra_field_and_missing_field(tmp_path):
    d = base_dplan()
    d["extra"] = 1
    del d["market_view"]["counter_evidence"]
    report = run(tmp_path, d)
    msgs = " ".join(i.message for i in report.errors)
    assert "SCHEMA" in errors(report) and "extra" in msgs and "counter_evidence" in msgs


def test_odd_lot_order_rejected_by_schema(tmp_path):
    d = base_dplan()
    d["orders"][0]["shares"] = 20_500
    assert "SCHEMA" in errors(run(tmp_path, d))


def test_team_id_mismatch_with_option(tmp_path):
    d = base_dplan()
    d["team_id"] = "TEAM_100"
    assert "TEAM_ID_MISMATCH" in errors(run(tmp_path, d))


# ---------------------------------------------------------------- 編號與引用

def test_leading_zero_id(tmp_path):
    d = base_dplan()
    d["sources"][0]["source_id"] = "S01"
    d["observations"][0]["source_ref"] = ["S01"]
    assert "ID_LEADING_ZERO" in errors(run(tmp_path, d))


def test_id_gap(tmp_path):
    d = base_dplan()
    d["observations"][1]["obs_id"] = "O3"
    d["market_view"]["basis_refs"] = ["O1", "O3"]
    d["market_view"]["logic"] = d["market_view"]["logic"].replace("O2", "O3")
    d["inferences"][1]["premise_refs"] = ["O3"]
    assert "ID_NOT_SEQUENTIAL" in errors(run(tmp_path, d))


def test_duplicate_id(tmp_path):
    d = base_dplan()
    d["inferences"][2]["inf_id"] = "I2"
    assert "ID_DUPLICATE" in errors(run(tmp_path, d))


def test_broken_reference(tmp_path):
    d = base_dplan()
    d["decisions"][1]["inference_refs"] = ["I9"]
    assert "REF_BROKEN" in errors(run(tmp_path, d))


def test_unused_source_and_text_mentions(tmp_path):
    d = base_dplan()
    d["sources"].append({"source_id": "S3", "authority": "twse", "url": "https://www.twse.com.tw/x",
                         "content_as_of": "2026-10-27T18:30:00+08:00"})
    d["inferences"][0]["logic"] += "（參考 O2）"
    report = run(tmp_path, d)
    assert {"SOURCE_UNUSED", "TEXT_MENTIONS_UNREFERENCED_OBS"} <= warns(report)


# ---------------------------------------------------------------- 時間

def test_lookahead_source(tmp_path):
    d = base_dplan()
    d["sources"][1]["content_as_of"] = "2026-10-28T09:30:00+08:00"
    assert {"SOURCE_LOOKAHEAD", "SOURCE_FUTURE_CONTENT"} <= errors(run(tmp_path, d, now=dt.datetime(2026, 10, 28, 10, 0, tzinfo=TPE)))


def test_completed_after_deadline_and_in_future(tmp_path):
    d = base_dplan()
    d["agent_metadata"]["run_completed_at"] = "2026-10-28T09:00:00+08:00"
    errs = errors(run(tmp_path, d))
    assert {"META_AFTER_DEADLINE", "META_COMPLETED_IN_FUTURE"} <= errs


def test_impossible_timestamp(tmp_path):
    d = base_dplan()
    d["agent_metadata"]["run_started_at"] = "2026-10-28T25:00:00+08:00"
    assert "TIME_INVALID" in errors(run(tmp_path, d))


def test_weekend_trade_date(tmp_path):
    d = base_dplan()
    d["trade_date"] = "2026-10-31"
    dp = base_datapack()
    dp["trade_date"] = "2026-10-31"
    assert "TRADE_DATE_WEEKEND" in errors(run(tmp_path, d, dp))


# ---------------------------------------------------------------- 內容品質

def test_observation_with_inference_words(tmp_path):
    d = base_dplan()
    d["observations"][0]["statement"] = "TAIEX 收 23,412.5，因此半導體會轉弱"
    assert "OBS_HAS_INFERENCE" in warns(run(tmp_path, d))


def test_media_without_archive_and_authority_domain(tmp_path):
    d = base_dplan()
    del d["sources"][1]["archive_url"]
    d["sources"][0]["authority"] = "tpex"
    w = warns(run(tmp_path, d))
    assert {"SOURCE_NO_ARCHIVE", "SOURCE_AUTHORITY_MISMATCH"} <= w


# ---------------------------------------------------------------- 決策覆蓋

def test_coverage_missing(tmp_path):
    d = base_dplan()
    d["no_trade_decisions"].pop()
    assert "COVERAGE_MISSING" in errors(run(tmp_path, d))


def test_no_trade_for_stock_not_held(tmp_path):
    d = base_dplan()
    d["no_trade_decisions"].append({"ticker": "2892", "reason_refs": ["I3"]})
    assert "NO_TRADE_NOT_HELD" in errors(run(tmp_path, d))


def test_receivable_only_is_not_a_holding(tmp_path):
    dp = base_datapack()
    dp["holdings"].append({"code": "6669", "shares": 0, "receivable_shares": 9913, "close_price": 2610.0, "tradable_weight": None})
    report = run(tmp_path, None, dp)
    assert report.ok, report.format_text()
    d = base_dplan()
    d["no_trade_decisions"].append({"ticker": "6669", "reason_refs": ["I3"]})
    report = run(tmp_path, d, dp)
    assert "NO_TRADE_NOT_HELD" in errors(report)
    assert any("應收配股" in i.message for i in report.errors)


def test_ticker_in_both_lists(tmp_path):
    d = base_dplan()
    d["no_trade_decisions"].append({"ticker": "2330", "reason_refs": ["I1"]})
    assert "DECISION_AND_NO_TRADE" in errors(run(tmp_path, d))


def test_action_semantics(tmp_path):
    d = base_dplan()
    d["decisions"][1]["action"] = "ADD"
    assert "ACTION_NOT_HELD" in errors(run(tmp_path, d))
    d = base_dplan()
    d["decisions"][0]["action"] = "BUY"
    assert "ACTION_BUY_BUT_HELD" in errors(run(tmp_path, d))


def test_trim_to_zero_should_be_sell_all(tmp_path):
    d = base_dplan()
    d["decisions"][0]["target_weight"] = 0
    assert "DECISION_ZERO_WEIGHT" in errors(run(tmp_path, d))


# ---------------------------------------------------------------- 委託推導

def test_order_shares_mismatch(tmp_path):
    d = base_dplan()
    d["orders"][0]["shares"] = 30_000
    assert "ORDER_DERIVATION_MISMATCH" in errors(run(tmp_path, d))


def test_order_missing(tmp_path):
    d = base_dplan()
    d["orders"].pop()
    assert "ORDER_MISSING" in errors(run(tmp_path, d))


def test_oversell(tmp_path):
    d = base_dplan()
    d["orders"][0]["shares"] = 250_000
    assert "OVERSELL" in errors(run(tmp_path, d))


def test_missing_price_for_new_buy(tmp_path):
    assert "PRICE_MISSING" in errors(run(tmp_path, prices={}))


def test_float_edge_warning(tmp_path):
    d = base_dplan()
    d["decisions"][1]["target_weight"] = 0.0157
    d["orders"][1]["shares"] = 314_000
    d["market_view"]["posture"]["net_exposure_intent"] = "hold"
    assert "DERIVATION_FLOAT_EDGE" in warns(run(tmp_path, d))


def test_decision_without_order_effect(tmp_path):
    d = base_dplan()
    d["decisions"][0]["target_weight"] = 0.2
    d["orders"] = [d["orders"][1]]
    report = run(tmp_path, d)
    assert "DECISION_NO_ORDER" in warns(report)


# ---------------------------------------------------------------- 交易後合規

def test_over_cap_buy(tmp_path):
    d = base_dplan()
    d["decisions"][1]["target_weight"] = 0.11
    assert "DECISION_OVER_CAP" in errors(run(tmp_path, d))


def test_ticker_not_in_universe(tmp_path):
    d = base_dplan()
    d["decisions"][1]["ticker"] = "9999"
    d["orders"][1]["ticker"] = "9999"
    assert "TICKER_NOT_IN_UNIVERSE" in errors(run(tmp_path, d, prices={"9999": 50.0}))


def test_posture_reduce_but_flat(tmp_path):
    d = base_dplan()
    d["market_view"]["posture"]["net_exposure_intent"] = "reduce"
    assert "POSTURE_NET_FLOW" in errors(run(tmp_path, d))


def test_posture_hold_beyond_tolerance(tmp_path):
    d = base_dplan()
    d["decisions"][1]["target_weight"] = 0.05
    d["orders"][1]["shares"] = 1_000_000
    assert "POSTURE_NET_FLOW" in errors(run(tmp_path, d))


def test_cash_outside_declared_range(tmp_path):
    d = base_dplan()
    d["market_view"]["posture"]["target_cash_pct_range"] = [0.12, 0.2]
    assert "POSTURE_CASH_RANGE" in errors(run(tmp_path, d))


def test_cash_negative(tmp_path):
    d = base_dplan()
    d["decisions"][1]["target_weight"] = 0.1
    d["orders"][1]["shares"] = 2_000_000
    d["market_view"]["posture"]["net_exposure_intent"] = "increase"
    d["decisions"].append({"decision_id": "D3", "ticker": "2892", "action": "BUY", "target_weight": 0.1, "inference_refs": ["I2"]})
    d["orders"].append({"ticker": "2892", "side": "BUY", "shares": 3_333_000, "decision_ref": "D3"})
    assert "CASH_NEGATIVE" in errors(run(tmp_path, d))


def test_holdings_count_below_minimum(tmp_path):
    d = base_dplan()
    for idx, code in enumerate(OTHERS[:4], start=3):
        d["no_trade_decisions"] = [n for n in d["no_trade_decisions"] if n["ticker"] != code]
        d["decisions"].append({"decision_id": f"D{idx}", "ticker": code, "action": "SELL_ALL", "target_weight": 0, "inference_refs": ["I3"]})
        d["orders"].append({"ticker": code, "side": "SELL", "shares": 333_000, "decision_ref": f"D{idx}"})
    d["market_view"]["posture"] = {"net_exposure_intent": "reduce", "target_cash_pct_range": [0.03, 0.25]}
    assert "HOLDINGS_COUNT" in errors(run(tmp_path, d))


def test_funding_without_gap(tmp_path):
    d = base_dplan()
    d["decisions"][0]["funding_for"] = ["D2"]
    d["inferences"][0]["logic"] += "，釋出資金支應 D2"
    assert "FUNDING_NO_GAP" in errors(run(tmp_path, d))


def test_funding_on_buy(tmp_path):
    d = base_dplan()
    d["decisions"][1]["funding_for"] = ["D1"]
    assert {"FUNDING_ON_NON_SELL", "FUNDING_TARGET_NOT_BUY"} <= errors(run(tmp_path, d))


def test_passive_overweight_deadline(tmp_path):
    dp = base_datapack()
    dp["holdings"][1]["shares"] = 1_100_000
    dp["account"]["cash"] -= 767_000 * 100
    dp["account"]["nav_tradable"] = dp["account"]["cash"] + sum(h["shares"] * h["close_price"] for h in dp["holdings"])
    days = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-27"]  # 10/23、10/26 休市
    dp["compliance"]["incidents"] = [
        {"date": day, "check_code": "post_trade_weight", "status": "warn", "message": "2317 權重 10.6% 超過上限"} for day in days
    ]
    d = base_dplan()
    d["market_view"]["posture"]["target_cash_pct_range"] = [0.0, 0.2]
    report = run(tmp_path, d, dp)
    assert "PASSIVE_OVERWEIGHT_DEADLINE" in errors(report), report.format_text()


def test_active_share_too_low(tmp_path):
    etf = tmp_path / "etf.json"
    portfolio_like = {"2330": 0.18, **{c: 0.0333 for c in OTHERS[:14]}}
    etf.write_text(json.dumps({"00980A": portfolio_like}), encoding="utf-8")
    report = run(tmp_path, active_etf_holdings_path=etf)
    assert "ACTIVE_SHARE_TOO_LOW" in errors(report)


def test_theme_weight_low(tmp_path):
    theme = tmp_path / "theme.json"
    theme.write_text(json.dumps({"name": "金融", "tickers": ["2881", "2882", "2885"], "min_weight": 0.5}), encoding="utf-8")
    assert "THEME_WEIGHT_LOW" in warns(run(tmp_path, theme_path=theme))


# ---------------------------------------------------------------- 資料包狀態

def test_datapack_not_settled(tmp_path):
    dp = base_datapack()
    dp.update(missing=["prev_settlement"], account=None, holdings=None, as_of_date=None)
    assert "DATAPACK_NOT_SETTLED" in errors(run(tmp_path, None, dp))


def test_datapack_wrong_date(tmp_path):
    dp = base_datapack()
    dp["trade_date"] = "2026-10-27"
    assert "DATAPACK_DATE_MISMATCH" in errors(run(tmp_path, None, dp))


def test_last_chance_violation_warning(tmp_path):
    dp = base_datapack()
    dp["compliance"]["violations"] = 2
    assert "VIOLATIONS_LAST_CHANCE" in warns(run(tmp_path, None, dp))


def test_without_datapack_skips_portfolio_checks(tmp_path):
    report = run(tmp_path, use_datapack=False)
    assert report.ok
    assert any("資料包" in s for s in report.skipped)


@pytest.mark.parametrize("cli_args", [[], ["--json"]])
def test_cli_exit_codes(tmp_path, cli_args, capsys):
    from dplan_checker.__main__ import main
    good = tmp_path / f"D-Plan_{TEAM}_{TRADE_DATE}.json"
    good.write_text(json.dumps(base_dplan(), ensure_ascii=False), encoding="utf-8")
    assert main([str(good), "--team-id", TEAM, "--now", "2026-10-28T07:30:00", *cli_args]) == 0
    bad = copy.deepcopy(base_dplan())
    bad["decisions"][1]["inference_refs"] = ["I9"]
    good.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    assert main([str(good), "--team-id", TEAM, "--now", "2026-10-28T07:30:00", *cli_args]) == 1
