"""財報資料工具測試（不連網：假的 HTTP client 回傳固定內容）。"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from agent.datapack import first_day_datapack
from agent.run_daily import execute
from agent.settings import ThemeRule, ThemeSettings
from agent.tools.fundamentals import (FundamentalsLoader, Fundamentals, Season, latest_ended, num,
                                      parse_mops_summary)
from agent.tools.http import FetchError, Fetched, HttpClient

from test_pipeline import CAL, TRADE_DATE, build_ctx, errors


def summary_html(rows: list[list[str]], header: list[str]) -> str:
    def tr(cells, tag="td"):
        return "<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>"
    return "<html><table>" + tr(header, "th") + "".join(tr(r) for r in rows) + "</table></html>"


INCOME_HEADER = ["公司代號", "公司名稱", "營業收入", "本期淨利（淨損）", "淨利（淨損）歸屬於母公司業主", "基本每股盈餘（元）"]
BANK_HEADER = ["公司代號", "公司名稱", "收益", "本期淨利（淨損）", "淨利（損）歸屬於母公司業主", "基本每股盈餘（元）"]
CASH_HEADER = ["公司代號", "公司名稱", "營業活動之淨現金流入（流出）", "投資活動之淨現金流入（流出）"]


class FakeClient:
    def __init__(self, pages: dict[str, str], jsons: dict[str, list]):
        self.pages, self.jsons, self.calls = pages, jsons, []

    def get_text(self, url, refresh=False, tag="", encoding="utf-8"):
        self.calls.append((url, tag))
        for key, html in self.pages.items():
            if key in url:
                return Fetched(url, html, "2026-10-26T19:00:00+08:00", Path("snap"), False)
        raise FetchError(f"404 {url}")

    def get_json(self, url, refresh=False, tag=""):
        self.calls.append((url, tag))
        for key, data in self.jsons.items():
            if key in url:
                return Fetched(url, data, "2026-10-26T19:00:00+08:00", Path("snap"), False)
        raise FetchError(f"404 {url}")


def test_season_logic():
    assert latest_ended(dt.date(2026, 10, 22)) == Season(115, 3)
    assert latest_ended(dt.date(2026, 4, 1)) == Season(115, 1)
    assert latest_ended(dt.date(2026, 3, 31)) == Season(114, 4)
    assert Season(115, 1).prev() == Season(114, 4)
    assert Season(114, 4).deadline == dt.date(2026, 3, 31)
    assert Season(115, 2).final(dt.date(2026, 10, 22)) and not Season(115, 3).final(dt.date(2026, 11, 15))
    assert Season(115, 2).label == "115 年上半年" and Season(114, 4).end == dt.date(2025, 12, 31)


def test_num_and_parse_summary():
    assert num("1,234.5") == 1234.5 and num("--") is None and num("-79,817") == -79817
    html = (summary_html([["2330", "台積電", "1,000", "900", "880", "33.5"], ["9999", "x", "1", "1", "1", "1"]], INCOME_HEADER)
            + summary_html([["2882", "國泰金", "500", "120", "118", "5.2"]], BANK_HEADER)
            + summary_html([["2330", "台積電", "1,200", "-300"]], CASH_HEADER))
    rows = parse_mops_summary(html, {"2330", "2882"})
    assert rows["2330"]["eps"] == 33.5 and rows["2330"]["ni_parent"] == 880 and rows["2330"]["ocf"] == 1200
    assert rows["2882"]["ni_parent"] == 118 and "9999" not in rows


def _loader(tmp_path, with_q3=False):
    def inc(eps, ni):
        return summary_html([["2330", "台積電", "1", str(ni), str(ni), str(eps)],
                             ["2882", "國泰金", "1", "-50", "-50", "-0.4"]], INCOME_HEADER)
    pages = {
        "t163sb04?encodeURIComponent=1&step=1&firstin=1&off=1&isQuery=Y&TYPEK=sii&year=115&season=02": inc(30, 800_000),
        "t163sb04?encodeURIComponent=1&step=1&firstin=1&off=1&isQuery=Y&TYPEK=sii&year=114&season=02": inc(20, 520_000),
        "t163sb04?encodeURIComponent=1&step=1&firstin=1&off=1&isQuery=Y&TYPEK=sii&year=114&season=04": inc(45, 1_200_000),
        "t163sb20?encodeURIComponent=1&step=1&firstin=1&off=1&isQuery=Y&TYPEK=sii&year=115&season=02":
            summary_html([["2330", "台積電", "960,000", "-1"]], CASH_HEADER),
        "t21/sii/t21sc03_115_9_0": "<table><tr><td>2330</td><td>台積電</td><td>1</td><td>1</td><td>1</td><td>0</td>"
                                   "<td>54.64</td><td>1</td><td>1</td><td>41.10</td><td>-</td></tr></table>",
    }
    if with_q3:
        pages["TYPEK=sii&year=115&season=03"] = summary_html([["2330", "台積電", "1", "1300000", "1300000", "50"]], INCOME_HEADER)
        pages["TYPEK=sii&year=114&season=03"] = summary_html([["2330", "台積電", "1", "850000", "850000", "33"]], INCOME_HEADER)
    jsons = {
        "BWIBBU_ALL": [{"Date": "1151022", "Code": "2330", "PEratio": "27.65", "DividendYield": "0.92", "PBratio": "9.61"},
                       {"Date": "1151022", "Code": "2882", "PEratio": "", "DividendYield": "3", "PBratio": "1.1"}],
        "t187ap03_L": [{"公司代號": "2330", "實收資本額": "259323700670", "普通股每股面額": "新台幣 10.0000元"},
                       {"公司代號": "2882", "實收資本額": "146000000000", "普通股每股面額": "新台幣 10.0000元"}],
    }
    client = FakeClient(pages, jsons)
    return FundamentalsLoader(client, tmp_path, {"2330": "twse", "2882": "twse"}, dt.date(2026, 10, 22)), client


def test_loader_ttm_and_metrics(tmp_path):
    loader, client = _loader(tmp_path)
    out = loader.load({"2330": 2385.0, "2882": 60.0})
    f = out["2330"]
    assert f.season == Season(115, 2) and f.eps_ytd == 30 and f.eps_ytd_prev == 20
    assert f.eps_ttm == 55 and f.ni_ttm == 800_000 + 1_200_000 - 520_000
    assert f.eps_growth == 50.0 and f.cash_conversion == 1.2
    assert f.rev_yoy == 54.64 and f.rev_cum_yoy == 41.10 and f.rev_month == "11509"
    assert f.pe == 27.65 and f.roe == round(9.61 / 27.65 * 100, 2)
    assert f.market_cap_rank == 1 and out["2882"].market_cap_rank == 2
    assert set(f.records) == {"income", "income_prev", "income_q4", "cashflow", "revenue", "valuation"}
    assert f.records["income"].authority == "mops" and f.records["valuation"].authority == "twse"
    assert f.records["revenue"].url.startswith("https://mopsov.twse.com.tw/")
    g = out["2882"]
    assert g.pe is None and g.profitable_ttm is False and g.eps_growth is None  # 去年同期虧損時沒有成長率
    # 已定案的季別永久快取（tag 空白）；還在申報期的 115Q3 每天一份
    tags = {url.split("season=")[1][:2] + url.split("year=")[1][:3]: tag for url, tag in client.calls if "season=" in url}
    assert tags["02115"] == "" and tags["03115"] == "2026-10-22"
    assert any("找不到" not in n for n in loader.notes) or not loader.notes


def test_loader_prefers_newest_season_per_company(tmp_path):
    loader, _ = _loader(tmp_path, with_q3=True)
    out = loader.load({"2330": 2385.0, "2882": 60.0})
    assert out["2330"].season == Season(115, 3) and out["2330"].eps_ttm == 50 + 45 - 33
    assert out["2882"].season == Season(115, 2)  # 2882 還沒公告第 3 季


def test_fundamentals_fact_has_no_inference_words(tmp_path):
    loader, _ = _loader(tmp_path)
    f = loader.load({"2330": 2385.0, "2882": 60.0})["2330"]
    ctx, _ = build_ctx(tmp_path / "ctx", first_day_datapack("TEAM_123", TRADE_DATE, "x"))
    key = f.add_fact(ctx, "台積電")
    fact = ctx.facts.get(key)
    assert "近四季合計 55 元" in fact.statement and "本益比 27.65 倍" in fact.statement and "54.64%" in fact.statement
    assert len(fact.sources) == 6 and fact.topic == "fundamentals"
    from dplan_checker import rules as R
    assert not [w for w in R.INFERENCE_WORDS if w in fact.statement]
    assert Fundamentals("1101", "twse").add_fact(ctx, "台泥") is None


def test_http_tag_keeps_daily_snapshots(tmp_path):
    client = HttpClient(tmp_path, offline=True)
    a, _ = client._paths("https://x/y", "2026-10-22")
    b, _ = client._paths("https://x/y", "2026-10-23")
    c, _ = client._paths("https://x/y")
    assert len({a, b, c}) == 3
    with pytest.raises(FetchError, match="離線"):
        client.get_text("https://x/y", tag="2026-10-22")


def test_pipeline_with_fundamentals_and_rule_theme(tmp_path):
    ctx, dp_path = build_ctx(tmp_path, first_day_datapack("TEAM_123", TRADE_DATE, "2026-10-26T19:40:00+08:00"))
    codes = sorted(ctx.universe)
    for i, c in enumerate(codes):
        f = Fundamentals(c, ctx.markets[c], Season(115, 2), eps_ytd=2.0 + i % 7, eps_ytd_prev=2.0, eps_ttm=4.0 + i % 7,
                         ni_ytd=1e6, ni_ttm=(-1e5 if i % 10 == 0 else 2e6), ocf_ytd=1.1e6, rev_month="11509",
                         rev_yoy=float(i % 30 - 10), rev_cum_yoy=float(i % 20 - 5), pe=15.0 + i % 20, pb=2.0 + i % 3,
                         valuation_date=dt.date(2026, 10, 22), market_cap_rank=i + 1)
        loader, _ = _loader(tmp_path / "l")
        f.records = loader.load({"2330": 1.0, "2882": 1.0})["2330"].records
        ctx.fundamentals[c] = f
    theme = ThemeSettings("優質動能", (), 0.6, "rule",
                          rules=(ThemeRule("ni_ttm", min=1), ThemeRule("market_cap_rank", max=100, held_max=120)))
    ctx.settings = ctx.settings.__class__(**{**ctx.settings.__dict__, "theme": theme})

    result = execute(ctx, dp_path, CAL, write_out=False)
    assert result.ok, errors(result)
    doc = json.loads(result.dplan_path.read_text(encoding="utf-8"))
    assert len(doc["observations"]) <= 100 and len(doc["sources"]) <= 50
    assert any(o["topic"] == "fundamentals" for o in doc["observations"])
    assert any(s["authority"] == "mops" for s in doc["sources"])
    members = {c for i, c in enumerate(codes) if i % 10 != 0 and i < 100}
    weights = {d["ticker"]: d["target_weight"] for d in doc["decisions"]}
    assert sum(w for c, w in weights.items() if c in members) >= 0.6
    assert not [i for i in result.report.issues if i.code in ("SOURCE_AUTHORITY_MISMATCH", "THEME_WEIGHT_LOW")]
