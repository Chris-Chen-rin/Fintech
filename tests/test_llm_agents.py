"""五個 LLM 版 Agent 串成整條流程（模型回應用 MockClient 模擬），D-Plan 要通過檢查器。"""
from __future__ import annotations

import datetime as dt
import json
import re

import agent.run_daily as run_daily
from agent.agents import LLM_AGENTS, RULE_AGENTS, STEPS
from agent.datapack import first_day_datapack
from agent.llm.providers import MockClient
from agent.settings import ModelSpec
from agent.sources import SourceRecord
from agent.tools.announcements import Announcement, parse_news_page
from agent.trading_calendar import TPE
from tests.test_pipeline import CAL, TRADE_DATE, build_ctx, errors

TOKEN = re.compile(r"\{(F\d+)\}")


def responder(system: str, user: str):
    if "情報分析師" in system:
        return {"facts": [{"idx": 0, "statement": "公告 2026 年 9 月營收 3310 億元，年增 30.5%",
                           "values": {"revenue_100m_twd": 3310, "revenue_yoy_pct": 30.5, "made_up": 999}},
                          {"idx": 1, "statement": "公告接獲訂單 777 億元", "values": {}}]}  # 777 不在原文，整則丟掉
    if "市場策略分析師" in system:
        keys = TOKEN.findall(user)
        return {"regime": "neutral", "stance": "neutral", "intent": "increase", "cash_target": 0.08,
                "cash_range": [0.05, 0.12], "basis": keys[:3],
                "logic": f"加權指數近期走勢（{{{keys[0]}}}）與上漲家數（{{{keys[-1]}}}）互有消長，研判中性，首日全現金需建倉。",
                "counter_evidence": "外資動向未納入", "confidence": 0.6}
    if "個股研究員" in system:
        out = []
        for block in user.split("■ ")[1:]:
            code = block.split()[0]
            score = float(re.search(r"量化分數 ([+-][\d.]+)", block).group(1))
            keys = TOKEN.findall(block)
            out.append({"ticker": code, "view": "positive" if score > -0.3 else "neutral", "score": score,
                        "premises": keys[:3], "logic": f"{code} 價量與基本面（{{{keys[0]}}}）顯示動能與品質兼具，維持觀察並依分數排序。",
                        "conclusion": "優質動能", "counter_evidence": None})
        return {"theses": out}
    if "反方審查員" in system:
        first = re.search(r"■ (T:\d+)", user)
        return {"reviews": [{"key": first.group(1), "verdict": "weak", "note": "短線漲幅已大"}] if first else []}
    if "投資組合經理" in system:
        max_out = int(re.search(r"非主題核心池的股票最多 (\d+) 檔", system).group(1)) if "非主題核心池" in system else 99
        cands = re.findall(r"^(\d{4}) .*? (核心池|衛星)：", user.split("候選")[1], re.M)
        core = [c for c, tag in cands if tag == "核心池"]
        sat = [c for c, tag in cands if tag == "衛星"][:max_out]
        buy = (core + sat)[:25]
        return {"sell": [], "buy": buy, "overweight": buy[:2], "underweight": [],
                "logic": "首日依研究分數與主題核心池優先建立持股，分散於多個產業，控制單一個股權重。"}
    raise AssertionError(system[:40])


def mock_agents(settings):
    spec = ModelSpec("openrouter", "mock/test-model")
    return {s: [LLM_AGENTS[s](spec, MockClient(spec, responder)), RULE_AGENTS[s]()] for s in STEPS}


def _news(ctx):
    key = "mops:news:2026-10-22"
    ctx.registry.add(SourceRecord(key, "mops", "https://mopsov.twse.com.tw/mops/web/ajax_t05st02?day=22",
                                  "2026-10-22T15:00:00+08:00", "重大訊息"))
    at = dt.datetime(2026, 10, 22, 14, 0, tzinfo=TPE)
    ctx.announcements = [
        Announcement("2330", "台積電", at, "公告本公司115年9月營收", "1.營收：3,310億元\n2.年增率：30.5%", key),
        Announcement("2317", "鴻海", at, "公告本公司取得訂單", "金額 100 億元", key),
    ]


def test_full_llm_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(run_daily, "build_agents", mock_agents)
    ctx, dp_path = build_ctx(tmp_path, first_day_datapack("TEAM_123", TRADE_DATE, "2026-10-26T19:40:00+08:00"))
    _news(ctx)
    result = run_daily.execute(ctx, dp_path, CAL, write_out=False)
    assert result.ok, errors(result)
    assert set(result.agents_used.values()) == {"llm"}, result.agents_used
    doc = json.loads(result.dplan_path.read_text(encoding="utf-8"))
    intel = [f for f in ctx.facts.all() if f.topic == "intel"]
    # made_up=999 不在原文被拿掉；「777 億元」的那則整則丟掉
    assert len(intel) == 1 and intel[0].values == {"revenue_100m_twd": 3310, "revenue_yoy_pct": 30.5, "days_before_trade": 5}
    assert intel[0].statement.startswith("2330 台積電 10/22 14:00 重大訊息：")
    assert "rule" not in doc["agent_metadata"]["model_version"]
    assert 22 <= len(doc["orders"]) <= 28


def test_parse_news_page():
    page = ("<form action='x'><input type='hidden' name='h00' value='台積電'><input type='hidden' name='h01' value='2330'>"
            "<input type='hidden' name='h02' value='20261022'><input type='hidden' name='h03' value='93005'>"
            "<input type='hidden' name='h04' value='公告 9 月營收'><input type='hidden' name='h08' value='營收&amp;3,310億元'></form>")
    [a] = parse_news_page(page, "k")
    assert a.code == "2330" and a.at == dt.datetime(2026, 10, 22, 9, 30, 5, tzinfo=TPE) and a.detail == "營收&3,310億元"


def test_repair_json():
    from agent.llm.base import parse_json_text

    # 字串內未跳脫的引號、多餘逗號
    assert parse_json_text('{"a": "他說"好"的", "b": [1, 2,],}') == {"a": '他說"好"的', "b": [1, 2]}
    # 輸出被截斷：退回最後一個完整元素
    assert parse_json_text('{"theses": [{"t": "2330", "v": 1}, {"t": "2317", "v"') == {"theses": [{"t": "2330", "v": 1}]}
    assert parse_json_text('說明文字 {"facts": [{"idx": 0, "statement": "營收 3310 億') == {"facts": [{"idx": 0}]}


def test_llm_call_budget(tmp_path, monkeypatch):
    import dataclasses

    from agent.llm.budget import daily_count

    monkeypatch.setattr(run_daily, "build_agents", mock_agents)
    ctx, dp_path = build_ctx(tmp_path, first_day_datapack("TEAM_123", TRADE_DATE, "2026-10-26T19:40:00+08:00"))
    ctx.settings = dataclasses.replace(ctx.settings, llm=dataclasses.replace(ctx.settings.llm, max_calls_per_run=2))
    result = run_daily.execute(ctx, dp_path, CAL, write_out=False)
    assert result.ok, errors(result)
    assert result.agents_used["intel"] == "llm" and result.agents_used["market"] == "llm"
    assert result.agents_used["research"] == "rule" and result.agents_used["portfolio_manager"] == "rule"
    assert daily_count(ctx.settings.paths.cache) == 2

    ctx2, dp2 = build_ctx(tmp_path / "b", first_day_datapack("TEAM_123", TRADE_DATE, "2026-10-26T19:40:00+08:00"))
    ctx2.settings = dataclasses.replace(ctx2.settings, paths=ctx.settings.paths,
                                        llm=dataclasses.replace(ctx2.settings.llm, daily_call_limit=2))
    result = run_daily.execute(ctx2, dp2, CAL, write_out=False)
    assert result.ok and set(result.agents_used.values()) == {"rule"}


def test_model_daily_limit_falls_through(tmp_path, monkeypatch):
    """稀缺模型（daily_limit）用完時改用下一個備援模型，不中斷。"""
    import dataclasses

    from agent.agents import RULE_AGENTS
    from agent.agents.llm_agents import LLMMarketAgent
    from agent.framework.runner import AgentRunner
    from agent.llm.budget import model_count

    ctx, _ = build_ctx(tmp_path, first_day_datapack("TEAM_123", TRADE_DATE, "x"))
    from agent.tools.quant import build_market_facts, build_stock_metrics
    ctx.metrics = build_stock_metrics(ctx)
    build_market_facts(ctx)
    flash = ModelSpec("google", "gemini-3.8-flash", daily_limit=1)
    lite = ModelSpec("google", "gemini-3.5-flash-lite")
    chain = [LLMMarketAgent(flash, MockClient(flash, responder)), LLMMarketAgent(lite, MockClient(lite, responder)),
             RULE_AGENTS["market"]()]
    inputs = {"market_facts": ctx.facts.of("market"), "market_metrics": ctx.market_metrics, "cash_ratio": 1.0,
              "first_day": True}
    runner = AgentRunner(ctx)
    runner.run("market", chain, inputs, lambda n, o: None)
    assert runner.models["market"][1] == "gemini-3.8-flash"
    runner.run("market2", chain, inputs, lambda n, o: None)
    assert runner.used["market2"] == "llm" and runner.models["market2"][1] == "gemini-3.5-flash-lite"
    assert model_count(ctx.settings.paths.cache, "market", flash) == 1


def test_fallback_list_settings(tmp_path):
    import yaml

    from agent.settings import EXAMPLE_SETTINGS_PATH, load_settings

    raw = yaml.safe_load(EXAMPLE_SETTINGS_PATH.read_text(encoding="utf-8"))
    raw["llm"]["agents"] = {"reviewer": {"provider": "google", "model": "gemini-3.8-flash", "api_key_env": "K", "daily_limit": 3}}
    raw["llm"]["fallback"] = {"reviewer": [{"provider": "google", "model": "gemini-3.5-flash-lite", "api_key_env": "K"},
                                           {"provider": "google", "model": "gemma-4-31b-it", "api_key_env": "K"}]}
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    s = load_settings(p)
    assert [m.model for m in s.llm.chain("reviewer")] == ["gemini-3.8-flash", "gemini-3.5-flash-lite", "gemma-4-31b-it"]
    assert s.llm.chain("reviewer")[0].daily_limit == 3
