"""多 Agent 流程整合測試：用合成行情（不連網）跑完整條流程，最後由 dplan_checker 驗收。"""
from __future__ import annotations

import datetime as dt
import json
import random
from pathlib import Path

import pytest
import yaml

from agent.datapack import DataPackError, find_datapack, first_day_datapack, parse_datapack
from agent.framework.context import FactBook, RunContext
from agent.framework.contracts import ContractError, Fact, validate_facts
from agent.framework.runner import AgentFailure, AgentRunner
from agent.orders import safe_weight
from agent.run_daily import _universe_markets, execute
from agent.settings import EXAMPLE_SETTINGS_PATH, load_settings
from agent.sources import SourceRegistry
from agent.tools.market_data import DayQuotes, MarketHistory, Quote, parse_twse
from agent.trading_calendar import TPE, TradingCalendar, now_tpe
from dplan_checker import rules as R
from dplan_checker.portfolio import target_shares

TRADE_DATE = dt.date(2026, 10, 27)
MOMENT = dt.datetime(2026, 10, 26, 20, 0, tzinfo=TPE)
CAL = TradingCalendar(R.KNOWN_HOLIDAYS)


def make_settings(tmp_path: Path, **risk):
    tmp_path.mkdir(parents=True, exist_ok=True)
    raw = yaml.safe_load(EXAMPLE_SETTINGS_PATH.read_text(encoding="utf-8"))
    raw["team_id"] = "TEAM_123"
    raw["paths"] = {"data_root": str(tmp_path)}
    raw["risk"].update(risk)
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return load_settings(path)


def synthetic_history(end: dt.date = dt.date(2026, 10, 22), n: int = 21, seed: int = 7,
                      base_seed: int | None = None) -> MarketHistory:
    """合成行情。base_seed 決定各股價位（相同時只有走勢不同），seed 決定走勢。"""
    universe, markets = _universe_markets()
    rng = random.Random(seed)
    base_rng = random.Random(seed if base_seed is None else base_seed)
    dates = [end]
    while len(dates) < n:
        dates.append(CAL.prev_trading_day(dates[-1]))
    dates.reverse()
    paths: dict[str, list[float]] = {}
    for code in universe:
        price = 2385.0 if code == R.TSMC else base_rng.choice([25.0, 48.0, 95.0, 180.0, 420.0, 880.0])
        drift = rng.uniform(-0.01, 0.01)
        series = [price]
        for _ in dates[1:]:
            series.append(round(max(1.0, series[-1] * (1 + drift + rng.gauss(0, 0.015))), 2))
        paths[code] = series
    days: dict[str, dict[dt.date, DayQuotes]] = {"twse": {}, "tpex": {}}
    taiex = 46000.0
    for i, day in enumerate(dates):
        prev_taiex = taiex
        taiex = round(taiex * (1 + rng.gauss(0, 0.01)), 2)
        for market in ("twse", "tpex"):
            quotes = {}
            for code, m in markets.items():
                if m != market:
                    continue
                close = paths[code][i]
                change = round(close - paths[code][i - 1], 2) if i else 0.0
                quotes[code] = Quote(close=close, change=change, value=5e9)
            indices = {"TAIEX": {"close": taiex, "chg_pct": round((taiex / prev_taiex - 1) * 100, 2),
                                 "chg_points": round(taiex - prev_taiex, 2)}} if market == "twse" else {}
            url = (f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={day:%Y%m%d}&type=ALLBUT0999&response=json"
                   if market == "twse" else
                   f"https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?date={day:%Y}%2F{day:%m}%2F{day:%d}&response=json")
            days[market][day] = DayQuotes(market, day, url, "2026-10-25T13:00:00+08:00", "synthetic", quotes, indices)
    return MarketHistory(dates, days)


def build_ctx(tmp_path: Path, raw_dp: dict, history: MarketHistory | None = None, **risk) -> tuple[RunContext, Path]:
    settings = make_settings(tmp_path, **risk)
    history = history or synthetic_history()
    universe, markets = _universe_markets()
    dp = parse_datapack(raw_dp, None, settings.team_id)
    prices = history.closes()
    prices.update(dp.close_prices())
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    dp_path = run_dir / "datapack.json"
    dp_path.write_text(json.dumps(raw_dp, ensure_ascii=False), encoding="utf-8")
    ctx = RunContext(settings=settings, window=CAL.window(dp.trade_date), datapack=dp, universe=universe, markets=markets,
                     history=history, prices=prices, registry=SourceRegistry(), facts=FactBook(), run_dir=run_dir,
                     started_at=MOMENT, clock_offset=MOMENT - now_tpe())
    return ctx, dp_path


def held_datapack(shares: dict[str, int], prices: dict[str, float], cash: float, trade_date: dt.date,
                  incidents: list | None = None) -> dict:
    equity = sum(s * prices[c] for c, s in shares.items())
    nav = cash + equity
    raw = first_day_datapack("TEAM_123", trade_date, "2026-10-27T18:58:00+08:00")
    raw["as_of_date"] = CAL.prev_trading_day(trade_date).isoformat()
    raw["prev_day_orders"]["outcome"] = "executed"
    raw["account"] = {"cash": cash, "cash_ratio_tradable": cash / nav, "nav_tradable": nav, "aum": nav,
                      "receivable_cash": 0.0, "holdings_count": len(shares)}
    raw["holdings"] = [{"code": c, "shares": s, "receivable_shares": 0, "close_price": prices[c],
                        "tradable_weight": s * prices[c] / nav} for c, s in shares.items()]
    raw["compliance"]["incidents"] = incidents or []
    return raw


def errors(result) -> list[str]:
    return [f"{i.code}: {i.message}" for i in result.report.errors]


# ---------------------------------------------------------------- 整條流程

def test_first_day_pipeline_passes_checker(tmp_path):
    ctx, dp_path = build_ctx(tmp_path, first_day_datapack("TEAM_123", TRADE_DATE, "2026-10-26T19:40:00+08:00"))
    result = execute(ctx, dp_path, CAL, write_out=True)
    assert result.ok, errors(result)
    doc = json.loads(result.dplan_path.read_text(encoding="utf-8"))
    assert result.out_path and result.out_path.name == "D-Plan_TEAM_123_2026-10-27.json"
    assert doc["market_view"]["posture"]["net_exposure_intent"] == "increase"
    assert 22 <= len(doc["decisions"]) <= 28 and all(d["action"] == "BUY" for d in doc["decisions"])
    assert not doc["no_trade_decisions"] and len(doc["orders"]) == len(doc["decisions"])
    assert doc["agent_metadata"]["model_version"] == "rule-based-agents-v1"
    assert (result.run_dir / "check_report.txt").exists()
    assert any(p.name.endswith("portfolio_manager_rule.json") for p in (result.run_dir / "agents").iterdir())
    for d in doc["decisions"]:
        assert d["target_weight"] <= (0.23 if d["ticker"] == R.TSMC else 0.09)


def _day1_holdings(tmp_path: Path) -> tuple[dict[str, int], dict[str, float], float]:
    ctx, dp_path = build_ctx(tmp_path / "d1", first_day_datapack("TEAM_123", TRADE_DATE, "2026-10-26T19:40:00+08:00"))
    result = execute(ctx, dp_path, CAL, write_out=False)
    doc = json.loads(result.dplan_path.read_text(encoding="utf-8"))
    shares = {o["ticker"]: o["shares"] for o in doc["orders"]}
    cost = sum(s * ctx.prices[c] for c, s in shares.items())
    return shares, ctx.prices, 1_000_000_000 - cost * (1 + R.FEE_RATE)


def test_second_day_covers_every_holding(tmp_path):
    shares, prices, cash = _day1_holdings(tmp_path)
    raw = held_datapack(shares, prices, cash, dt.date(2026, 10, 28))
    ctx, dp_path = build_ctx(tmp_path / "d2", raw)
    result = execute(ctx, dp_path, CAL, write_out=False)
    assert result.ok, errors(result)
    doc = json.loads(result.dplan_path.read_text(encoding="utf-8"))
    covered = {d["ticker"] for d in doc["decisions"]} | {n["ticker"] for n in doc["no_trade_decisions"]}
    assert set(shares) <= covered
    assert doc["market_view"]["posture"]["net_exposure_intent"] == "hold"
    assert len(doc["no_trade_decisions"]) >= 15  # 價格沒變，絕大多數持股落在無交易帶內


def test_passive_overweight_is_trimmed(tmp_path):
    shares, prices, cash = _day1_holdings(tmp_path)
    victim = next(c for c in shares if c != R.TSMC)
    nav = cash + sum(s * prices[c] for c, s in shares.items())
    extra = int(0.08 * nav / prices[victim] / 1000) * 1000
    shares[victim] += extra
    cash -= extra * prices[victim]
    raw = held_datapack(shares, prices, cash, dt.date(2026, 10, 28))
    ctx, dp_path = build_ctx(tmp_path / "ow", raw)
    assert ctx.datapack.weight(victim) > 0.10
    result = execute(ctx, dp_path, CAL, write_out=False)
    assert result.ok, errors(result)
    doc = json.loads(result.dplan_path.read_text(encoding="utf-8"))
    dec = next(d for d in doc["decisions"] if d["ticker"] == victim)
    assert dec["action"] == "TRIM" and dec["target_weight"] <= 0.09


def test_market_agent_revises_posture_from_portfolio_feedback(tmp_path):
    from agent.agents.market import RuleMarketAgent
    from agent.framework.contracts import validate_market_view
    from agent.tools.quant import build_market_facts, build_stock_metrics

    ctx, _ = build_ctx(tmp_path, first_day_datapack("TEAM_123", TRADE_DATE, "x"))
    ctx.metrics = build_stock_metrics(ctx)
    build_market_facts(ctx)
    inputs = {"market_facts": ctx.facts.of("market"), "market_metrics": ctx.market_metrics, "cash_ratio": 0.05,
              "first_day": False, "portfolio_feedback": {"net_flow_pct": -12.0, "cash_ratio": 0.17}}
    mv = RuleMarketAgent().run(ctx, inputs, ["POSTURE_NET_FLOW"])
    validate_market_view("market", mv, ctx.facts.keys())
    assert mv.intent == "reduce" and mv.cash_range[0] <= 0.17 <= mv.cash_range[1]
    assert "不可行" in mv.logic


# ---------------------------------------------------------------- 個別模組

def test_datapack_not_settled_raises():
    raw = first_day_datapack("TEAM_123", TRADE_DATE, "2026-10-26T19:40:00+08:00")
    raw.update(missing=["prev_settlement"], account=None, holdings=None)
    with pytest.raises(DataPackError, match="結算尚未產生"):
        parse_datapack(raw)


def test_datapack_team_mismatch_raises():
    with pytest.raises(DataPackError, match="不一致"):
        parse_datapack(first_day_datapack("TEAM_999", TRADE_DATE, "x"), team_id="TEAM_123")


def test_find_datapack_picks_latest(tmp_path):
    for i, gen in enumerate(["2026-10-26T18:00:00+08:00", "2026-10-26T19:45:00+08:00"]):
        (tmp_path / f"dp{i}.json").write_text(json.dumps(first_day_datapack("T", TRADE_DATE, gen)), encoding="utf-8")
    (tmp_path / "other.json").write_text(json.dumps(first_day_datapack("T", dt.date(2026, 10, 28), "z")), encoding="utf-8")
    assert find_datapack(tmp_path, TRADE_DATE).name == "dp1.json"
    with pytest.raises(DataPackError):
        find_datapack(tmp_path, dt.date(2026, 10, 29))


def test_safe_weight_avoids_float_edge():
    exact, floating = target_shares(0.0157, 1_000_000_000, 100)
    assert exact != floating  # 已知的浮點邊界
    w = safe_weight(0.0157, 1_000_000_000, 100)
    assert w < 0.0157 and len(set(target_shares(w, 1_000_000_000, 100))) == 1


def test_fact_with_inference_words_rejected():
    fact = Fact("F1", ["s1"], "台積電收 1000 元，因此看好後市", {"close": 1000}, "x", "2330")
    with pytest.raises(ContractError, match="推論用語"):
        validate_facts("intel", [fact], {"s1"})
    with pytest.raises(ContractError, match="不存在的來源"):
        validate_facts("intel", [Fact("F1", ["nope"], "台積電收 1000 元", {"close": 1000}, "x", "2330")], {"s1"})


class _Boom:
    name, variant = "pm", "llm"

    def run(self, ctx, inputs, feedback=None):
        raise RuntimeError("LLM 逾時")


class _Echo:
    name, variant = "pm", "rule"

    def run(self, ctx, inputs, feedback=None):
        return {"feedback": feedback}


def test_runner_falls_back_to_rule_agent(tmp_path):
    ctx, _ = build_ctx(tmp_path, first_day_datapack("TEAM_123", TRADE_DATE, "x"))
    runner = AgentRunner(ctx)
    out = runner.run("pm", [_Boom(), _Echo()], {}, lambda n, o: None)
    # LLM 版重試一次後降級；規則版只看原始 feedback
    assert runner.used["pm"] == "rule" and out["feedback"] is None
    assert len(list((ctx.run_dir / "agents").iterdir())) == 3
    out = runner.run("pm3", [_Boom(), _Echo()], {}, lambda n, o: None, feedback=["風控退回"])
    assert out["feedback"] == ["風控退回"]
    with pytest.raises(AgentFailure):
        runner.run("pm2", [_Boom()], {}, lambda n, o: None)


def test_parse_twse_signs():
    day = dt.date(2026, 10, 22)
    data = {"stat": "OK", "date": "20261022", "tables": [
        {"title": "價格指數(臺灣證券交易所)", "fields": ["指數", "收盤指數", "漲跌(+/-)", "漲跌點數", "漲跌百分比(%)", "特殊處理註記"],
         "data": [["發行量加權股價指數", "46,041.43", "<p style ='color:green'>-</p>", "846.34", "-1.81", ""]]},
        {"title": "每日收盤行情", "fields": ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價",
                                          "收盤價", "漲跌(+/-)", "漲跌價差", "最後揭示買價", "最後揭示買量", "最後揭示賣價", "最後揭示賣量", "本益比"],
         "data": [["2330", "台積電", "1,000", "10", "2,385,000", "2,400", "2,410", "2,380", "2,385",
                   "<p style= color:green>-</p>", "15.00", "", "", "", "", ""]]},
    ]}
    quotes, indices = parse_twse(data, day, {"2330"})
    assert indices["TAIEX"]["chg_pct"] == -1.81 and indices["TAIEX"]["chg_points"] == -846.34
    assert quotes["2330"].close == 2385 and quotes["2330"].change == -15
    assert parse_twse(data, dt.date(2026, 10, 23), {"2330"}) is None


def test_market_regime_hysteresis(tmp_path):
    from agent.agents.market import RuleMarketAgent
    from agent.tools.quant import build_market_facts, build_stock_metrics

    ctx, _ = build_ctx(tmp_path, first_day_datapack("TEAM_123", TRADE_DATE, "x"))
    ctx.metrics = build_stock_metrics(ctx)
    build_market_facts(ctx)
    mild = {"taiex_chg_pct": -0.5, "taiex_ret_5d": -1.0, "taiex_ret_20d": -1.0, "breadth": 0.45}  # 分數約 -0.48
    base = {"market_facts": ctx.facts.of("market"), "market_metrics": mild, "cash_ratio": 0.12, "first_day": False}
    assert RuleMarketAgent().run(ctx, base).regime == "neutral"
    mv = RuleMarketAgent().run(ctx, base | {"prev_regime": "risk_off"})
    assert mv.regime == "risk_off" and mv.intent == "hold" and "維持偏空" in mv.logic
    # 中性的現金區間上緣放寬：偏空後轉中性時 12% 現金不必立刻買回
    flat = base | {"market_metrics": {"taiex_chg_pct": 0.1, "taiex_ret_5d": 0.0, "taiex_ret_20d": 0.0, "breadth": 0.5}}
    mv = RuleMarketAgent().run(ctx, flat | {"prev_regime": "risk_off"})
    assert mv.regime == "neutral" and mv.intent == "hold" and mv.cash_range[1] >= 0.12


def test_rehearsal_fill_orders():
    from agent.rehearse import Book, fill_orders

    book = Book(cash=1_000_000.0, shares={"2330": 1000})
    prices = {"2330": 500.0, "2317": 100.0}
    outcome, cost = fill_orders(book, [{"ticker": "2330", "side": "SELL", "shares": 1000},
                                       {"ticker": "2317", "side": "BUY", "shares": 5000}], prices)
    assert outcome == "executed" and "2330" not in book.shares and book.shares["2317"] == 5000
    assert round(book.cash) == round(1_000_000 + 500_000 * (1 - R.FEE_RATE - R.TAX_RATE) - 500_000 * (1 + R.FEE_RATE))
    before = (book.cash, dict(book.shares))
    assert fill_orders(book, [{"ticker": "2317", "side": "BUY", "shares": 1_000_000}], prices)[0] == "void"
    assert (book.cash, book.shares) == before


def test_min_weight_floor(tmp_path):
    ctx, dp_path = build_ctx(tmp_path, first_day_datapack("TEAM_123", TRADE_DATE, "2026-10-26T19:40:00+08:00"),
                             min_weights={"2330": 0.15})
    result = execute(ctx, dp_path, CAL, write_out=False)
    assert result.ok, errors(result)
    doc = json.loads(result.dplan_path.read_text(encoding="utf-8"))
    dec = next(d for d in doc["decisions"] if d["ticker"] == "2330")
    assert dec["action"] == "BUY" and dec["target_weight"] >= 0.145


@pytest.mark.parametrize("floors", [{"2330": 0.30}, {"9999": 0.01}, {"2317": -0.01}])
def test_min_weight_invalid(tmp_path, floors):
    with pytest.raises(Exception, match="min_weights"):
        make_settings(tmp_path, min_weights=floors)


def test_waterfill_floor():
    from agent.agents.portfolio_manager import waterfill

    w = waterfill({"a": 1, "b": 1, "c": 1}, 0.9, {"a": 0.5, "b": 0.5, "c": 0.5}, {"a": 0.5})
    assert w["a"] == 0.5 and abs(w["b"] - 0.2) < 1e-9 and abs(sum(w.values()) - 0.9) < 1e-9
