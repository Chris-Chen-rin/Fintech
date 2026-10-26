"""策略參數（動能天數、品質比重、市值錨定）與回測搜尋。"""
from __future__ import annotations

import datetime as dt
import json

import pytest
import yaml

from agent.agents.portfolio_manager import anchor_weights
from agent.datapack import first_day_datapack
from agent.optimize import load_grid, summarize
from agent.rehearse import INITIAL_NAV
from agent.run_daily import execute
from agent.settings import EXAMPLE_SETTINGS_PATH, load_settings
from agent.tools.fundamentals import Fundamentals
from dplan_checker import rules as R

from test_pipeline import CAL, TRADE_DATE, build_ctx, errors


def strategy_settings(tmp_path, **strategy):
    tmp_path.mkdir(parents=True, exist_ok=True)
    raw = yaml.safe_load(EXAMPLE_SETTINGS_PATH.read_text(encoding="utf-8"))
    raw["team_id"] = "TEAM_123"
    raw["paths"] = {"data_root": str(tmp_path)}
    raw["strategy"].update(strategy)
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return load_settings(path)


def first_day_ctx(tmp_path, **strategy):
    ctx, dp_path = build_ctx(tmp_path, first_day_datapack("TEAM_123", TRADE_DATE, "2026-10-26T19:40:00+08:00"))
    ctx.settings = strategy_settings(tmp_path / "s", **strategy)
    return ctx, dp_path


@pytest.mark.parametrize("strategy, msg", [
    ({"momentum": {20: 0.5, 5: 0.4}}, "合計為 1"),
    ({"momentum": {200: 1.0}}, "1–120"),
    ({"anchor_top_n": 30}, "anchor_top_n"),
    ({"quality_share": 1.5}, "quality_share"),
])
def test_strategy_invalid(tmp_path, strategy, msg):
    with pytest.raises(Exception, match=msg):
        strategy_settings(tmp_path, **strategy)


def test_anchor_overflow_goes_elsewhere():
    class M:
        def __init__(self, cap):
            self.fund = Fundamentals("x", "twse", market_cap=cap)

    ctx = type("C", (), {"metrics": {"2330": M(80.0), "2317": M(20.0)}})()
    w = anchor_weights(ctx, ["2330", "2317"], 0.9, {"2330": 0.23, "2317": 0.09})
    assert w == {"2330": 0.23, "2317": 0.09}  # 超出上限的 0.58 不在錨定股之間重分


def test_momentum_lookbacks_follow_settings(tmp_path):
    ctx, dp_path = first_day_ctx(tmp_path, momentum={10: 0.5, 5: 0.5}, quality_share=0.0)
    result = execute(ctx, dp_path, CAL, write_out=False)
    assert result.ok, errors(result)
    m = next(iter(ctx.metrics.values()))
    assert m.ret(10) is not None and "（10 個交易日）" in ctx.facts.get(m.fact).statement
    doc = json.loads(result.dplan_path.read_text(encoding="utf-8"))
    logic = next(i["logic"] for i in doc["inferences"] if "動能分數" in i["logic"])
    assert "近 10 個交易日" in logic and "近 20 個交易日" not in logic


def test_anchor_top_n_first_day(tmp_path):
    ctx, dp_path = first_day_ctx(tmp_path, anchor_top_n=5)
    top = ["2330", "2317", "2454", "2308", "2382"]
    caps = {c: 1e12 / (i + 1) for i, c in enumerate(top)}
    for i, c in enumerate(sorted(ctx.universe)):
        f = Fundamentals(c, ctx.markets[c])
        f.market_cap = caps.get(c, 1e9 / (i + 1))
        ctx.fundamentals[c] = f
    ranked = sorted(ctx.fundamentals, key=lambda c: -ctx.fundamentals[c].market_cap)
    for r, c in enumerate(ranked, 1):
        ctx.fundamentals[c].market_cap_rank = r
    result = execute(ctx, dp_path, CAL, write_out=False)
    assert result.ok, errors(result)
    doc = json.loads(result.dplan_path.read_text(encoding="utf-8"))
    w = {d["ticker"]: d["target_weight"] for d in doc["decisions"]}
    assert set(top) <= set(w)
    assert w[R.TSMC] > 0.2 and all(w[c] > 0.08 for c in top[1:4])  # 依市值比例都超過上限，被截在上限
    construction = next(i for i in doc["inferences"] if "組合規則" in i["logic"])
    assert "市值前 5 大" in construction["logic"]


def test_summarize_split():
    rows = [{"trade_date": f"2026-09-{d:02d}", "ok": True, "nav": INITIAL_NAV * (1 + r / 100), "ret_pct": r,
             "taiex_ret_pct": t, "cost": 1e6, "turnover": 0.1, "orders": 1}
            for d, r, t in ((1, 0.0, 0.0), (2, 10.0, 5.0), (3, 0.0, 5.0))]
    s = summarize(rows, dt.date(2026, 9, 2))
    assert s["train_excess_pct"] == 5.0 and s["mdd_pct"] == round((1 / 1.1 - 1) * 100, 2)
    assert s["test_ret_pct"] == round((1 / 1.1 - 1) * 100, 2) and s["test_excess_pct"] == s["test_ret_pct"]
    assert s["cost_pct"] == 0.3


def test_window_metrics_and_robust_ranking():
    from agent.optimize import WINDOW, robust_ranking

    rows = [{"trade_date": (dt.date(2026, 4, 1) + dt.timedelta(days=i)).isoformat(), "ok": True,
             "nav": INITIAL_NAV * (1 + i / 100), "ret_pct": float(i), "taiex_ret_pct": i / 2, "cost": 0, "turnover": 0}
            for i in range(WINDOW + 5)]
    s = summarize(rows, None)
    assert s["w24_win_pct"] == 100.0 and s["w24_median_pct"] > 0 and s["w24_worst_pct"] > 0

    def r(name, med):
        return {"name": name, "w24_median_pct": med}
    ranked = robust_ranking([r("A／q1／a1", 5.0), r("A／q2／a1", 1.0), r("B／q1／a1", 3.0), r("B／q2／a2", 9.0)])
    assert [(x[2]["name"], x[0], x[1]) for x in ranked] == [("B／q1／a1", 4.0, 5.0), ("A／q1／a1", 3.5, 2.0),
                                                             ("A／q2／a1", 3.0, 5.0)]
    assert all(x[2]["name"] != "B／q2／a2" for x in ranked)


def test_format_report_sections():
    from agent.optimize import format_report

    def row(name, train, test, anchor):
        return {"name": name, "strategy": {"momentum": {"20": 1.0}, "quality_share": 0.5, "anchor_top_n": anchor,
                                           "max_swaps": 3},
                "ret_pct": train + test, "taiex_pct": 10.0, "excess_pct": train + test - 10, "mdd_pct": -5.0,
                "cost_pct": 1.0, "turnover_avg_pct": 2.0, "train_excess_pct": train, "test_excess_pct": test,
                "navs": [["2026-09-01", 1], ["2026-09-03", 1]]}
    rows = [row("只看20日／品質50%／前0大／換3", 3, -3, 0), row("只看20日／品質50%／前5大／換3", 2, -1, 5),
            row("只看20日／品質50%／前10大／換3", 1, 2, 10)]
    text = format_report(rows, dt.date(2026, 9, 2))
    for part in ("## 結果表", "## 欄位說明", "## 名稱怎麼讀", "## 統整", "20 日 100%", "相關係數 -1.00", "前 10 大"):
        assert part in text


def test_grid_exclude(tmp_path):
    from agent.settings import SettingsError

    s = strategy_settings(tmp_path)
    grid = tmp_path / "g.yaml"
    grid.write_text("momentum: {A: {20: 1.0}}\nquality_share: [0.0, 0.5]\nexclude: [A／品質0%／前0大／換3]\n",
                    encoding="utf-8")
    assert [n for n, _ in load_grid(grid, s)] == ["A／品質50%／前0大／換3"]
    grid.write_text("momentum: {A: {20: 1.0}}\nexclude: [B／品質0%／前0大／換3]\n", encoding="utf-8")
    with pytest.raises(SettingsError, match="exclude"):
        load_grid(grid, s)


def test_grid_file_parses(tmp_path):
    from agent.optimize import GRID_FILE

    combos = load_grid(GRID_FILE, strategy_settings(tmp_path))
    assert len(combos) == 54 and len({n for n, _ in combos}) == 54
