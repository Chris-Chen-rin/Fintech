"""多模型 LLM 骨架與主題模式的測試（不連網）。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.agents.llm_base import LLMAgent
from agent.agents.market import RuleMarketAgent
from agent.datapack import first_day_datapack
from agent.llm.base import LLMError, parse_json_text
from agent.llm.metadata import model_metadata
from agent.llm.providers import MockClient, make_client
from agent.run_daily import execute
from agent.settings import ModelSpec, ThemeRule, ThemeSettings
from agent.theme import resolve_theme, universe_industries

from test_pipeline import CAL, TRADE_DATE, build_ctx, errors


# ---------------------------------------------------------------- JSON 解析

@pytest.mark.parametrize("text", ['{"a": 1}', '```json\n{"a": 1}\n```', '好的，結果如下：\n{"a": 1}\n以上'])
def test_parse_json_text(text):
    assert parse_json_text(text) == {"a": 1}


def test_parse_json_text_rejects_garbage():
    with pytest.raises(LLMError):
        parse_json_text("沒有 JSON")
    with pytest.raises(LLMError):
        parse_json_text("{壞掉的 JSON}")


def test_make_client_by_provider():
    assert make_client(ModelSpec("xai", "grok-x", "K")).base == "https://api.x.ai/v1"
    assert make_client(ModelSpec("openai_compatible", "m", base_url="http://h:8000/v1/")).base == "http://h:8000/v1"
    assert type(make_client(ModelSpec("anthropic", "c", "K"))).__name__ == "AnthropicClient"
    assert type(make_client(ModelSpec("google", "g", "K"))).__name__ == "GeminiClient"
    with pytest.raises(LLMError):
        make_client(ModelSpec("mock", "m"))


def test_openrouter_provider_and_json_mode(monkeypatch):
    import agent.llm.providers as P

    assert ModelSpec("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free", "K").dplan_provider == "nvidia"
    assert ModelSpec("openrouter", "thinkingmachines/inkling:free", "K").dplan_provider == "other"
    sent = {}

    def fake_post(url, headers, body, timeout, **_):
        sent.update(url=url, body=body)
        return {"model": "thinkingmachines/inkling:free", "usage": {"prompt_tokens": 5, "completion_tokens": 7},
                "choices": [{"message": {"content": "```json\n{\"ok\": true}\n```"}}]}

    monkeypatch.setattr(P, "post_json", fake_post)
    monkeypatch.setenv("K", "secret")
    client = make_client(ModelSpec("openrouter", "thinkingmachines/inkling:free", "K", json_mode=False, extra={"seed": 42}))
    result = client.complete_json("系統", "使用者")
    assert sent["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert "response_format" not in sent["body"] and sent["body"]["seed"] == 42
    assert result.data == {"ok": True} and result.output_tokens == 7
    monkeypatch.setattr(P, "post_json", lambda *a, **k: {"error": {"code": 429, "message": "rate limited"}})
    with pytest.raises(LLMError, match="rate limited"):
        client.complete_json("系統", "使用者")


def _http_error(code):
    import io
    import urllib.error

    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b"busy"))


def test_post_json_busy_is_final_and_counts_retries(monkeypatch):
    import agent.llm.base as B

    sent = []
    monkeypatch.setattr(B.time, "sleep", lambda s: None)
    monkeypatch.setattr(B.urllib.request, "urlopen", lambda *a, **k: sent.append(1) or (_ for _ in ()).throw(_http_error(503)))
    retried = []
    with pytest.raises(B.Unavailable) as info:
        B.post_json("http://x", {}, {}, 1, retries=3, on_retry=lambda: retried.append(1))
    assert info.value.final and len(sent) == 3 and len(retried) == 2
    sent.clear()
    monkeypatch.setattr(B.urllib.request, "urlopen", lambda *a, **k: sent.append(1) or (_ for _ in ()).throw(_http_error(400)))
    with pytest.raises(LLMError) as info:
        B.post_json("http://x", {}, {}, 1)
    assert not isinstance(info.value, B.Unavailable) and len(sent) == 1


def test_scarce_model_sends_once(monkeypatch):
    import agent.llm.providers as P

    seen = {}
    monkeypatch.setattr(P, "post_json", lambda *a, retries, on_retry: seen.setdefault(a[0], retries) and {})
    monkeypatch.setenv("K", "secret")
    for model, limit in (("scarce", 3), ("lite", None)):
        with pytest.raises(LLMError):
            make_client(ModelSpec("google", model, "K", daily_limit=limit)).complete_json("s", "u")
    assert [seen[u] for u in sorted(seen, key=lambda u: "lite" in u)] == [1, 3]


# ---------------------------------------------------------------- agent_metadata

ALL = ("intel", "market", "research", "reviewer", "portfolio_manager")


def test_metadata_all_rule():
    assert model_metadata({s: "rule" for s in ALL}, {})[:2] == ("other", "rule-based-agents-v1")


def test_metadata_single_model():
    used = {s: "llm" for s in ALL}
    models = {s: ("google", "gemini-2.5-flash") for s in ALL}
    assert model_metadata(used, models)[:2] == ("google", "gemini-2.5-flash")


def test_metadata_mixed_providers_and_rule_fallback():
    used = {"intel": "rule", "market": "llm", "research": "llm", "reviewer": "rule", "portfolio_manager": "llm"}
    models = {"market": ("openai", "gpt-5-mini"), "research": ("google", "gemini-x"), "portfolio_manager": ("anthropic", "claude-x")}
    provider, version, mapping = model_metadata(used, models)
    assert provider == "other"
    assert version == "pm:claude-x;mk:gpt-5-mini;rs:gemini-x;rv:rule;in:rule"
    assert mapping["pm"] == "anthropic:claude-x" and mapping["in"] == "rule"


def test_metadata_same_provider_different_models():
    used = {"market": "llm", "portfolio_manager": "llm"}
    models = {"market": ("google", "gemini-flash"), "portfolio_manager": ("google", "gemini-pro")}
    assert model_metadata(used, models)[:2] == ("google", "pm:gemini-pro;mk:gemini-flash")


def test_metadata_too_long_is_hashed():
    used = {s: "llm" for s in ALL}
    models = {s: (p, f"{p}-very-long-model-name-2026") for s, p in zip(ALL, ("openai", "google", "xai", "deepseek", "anthropic"))}
    provider, version, mapping = model_metadata(used, models)
    assert provider == "other" and version.startswith("multi-model:") and 3 <= len(version) <= 60
    assert len(mapping) == 5
    assert model_metadata(used, models)[1] == version  # 同樣設定必須得到同樣版本字串


def test_metadata_retry_step_counts_as_same_agent():
    used = {"market": "llm", "market_retry": "rule", "portfolio_manager": "rule"}
    provider, version, _ = model_metadata(used, {"market": ("openai", "gpt-x")})
    assert (provider, version) == ("openai", "pm:rule;mk:gpt-x")  # 任何一輪用過 LLM 就算該 Agent 用了 LLM


# ---------------------------------------------------------------- 主題模式

def _theme(mode, **kw) -> ThemeSettings:
    return ThemeSettings(kw.pop("name", "測試主題"), tuple(kw.pop("tickers", ())), kw.pop("min_weight", 0.0), mode, **kw)


def test_theme_tickers_and_none():
    m = resolve_theme(_theme("tickers", tickers=["2330", "2454"]), {}, set())
    assert m.members == {"2330", "2454"} and m.active
    m = resolve_theme(_theme("none"), {}, set())
    assert not m.active and m.score("2330") == 0.0


def test_theme_industries():
    inds = universe_industries()
    m = resolve_theme(_theme("industries", industries=("金融保險業",)), {}, set())
    assert m.members == {c for c, i in inds.items() if i == "金融保險業"}
    assert len(m.members) == 16 and all("金融保險業" in r for r in m.reasons.values())


def test_theme_rule_with_hysteresis():
    metrics = {"A": SimpleNamespace(ret_20d=6.0), "B": SimpleNamespace(ret_20d=2.0), "C": SimpleNamespace(ret_20d=-1.0)}
    rule = ThemeRule("ret_20d", min=5.0, held_min=0.0)
    m = resolve_theme(_theme("rule", rules=(rule,)), metrics, held={"B", "C"})
    assert m.members == {"A", "B"}  # B 已持有、只要不跌破 0% 就留著；C 跌破
    assert "20 日報酬 6.00%" in m.reasons["A"]


def test_theme_llm_membership_file(tmp_path):
    path = tmp_path / "membership.json"
    path.write_text(json.dumps({"scores": {"2330": {"score": 0.9, "reason": "AI 晶片代工"}, "2882": 0.2, "2454": 0.6}}),
                    encoding="utf-8")
    m = resolve_theme(_theme("llm", membership_file=path), {}, set())
    assert m.members == {"2330", "2454"} and m.score("2882") == 0.2 and m.reasons["2330"] == "AI 晶片代工"
    m = resolve_theme(_theme("llm", membership_file=tmp_path / "nope.json"), {}, set())
    assert not m.active and m.notes


# ---------------------------------------------------------------- 接上整條流程

class _MockLLMMarket(LLMAgent):
    """把規則版包成 LLM 版，只驗證管線（token、模型紀錄、metadata）。"""
    name = "market"

    def __init__(self, spec):
        super().__init__(spec, MockClient(spec, lambda s, u: {"note": "ok"}))

    def build_prompt(self, ctx, inputs, feedback):
        return "你是市場分析師", json.dumps({"facts": len(inputs["market_facts"])})

    def parse(self, ctx, inputs, data):
        assert data == {"note": "ok"}
        return RuleMarketAgent().run(ctx, inputs, None)


class _BrokenLLMPM(LLMAgent):
    name = "portfolio_manager"

    def __init__(self, spec):
        super().__init__(spec, MockClient(spec, lambda s, u: "這不是 JSON"))

    def build_prompt(self, ctx, inputs, feedback):
        return "你是經理人", "{}"

    def parse(self, ctx, inputs, data):
        raise AssertionError("不應執行到這裡")


def test_pipeline_with_llm_agents_and_industry_theme(tmp_path, monkeypatch):
    import agent.agents as agents_mod

    monkeypatch.setitem(agents_mod.LLM_AGENTS, "market", _MockLLMMarket)
    monkeypatch.setitem(agents_mod.LLM_AGENTS, "portfolio_manager", _BrokenLLMPM)
    ctx, dp_path = build_ctx(tmp_path, first_day_datapack("TEAM_123", TRADE_DATE, "2026-10-26T19:40:00+08:00"))
    llm = ctx.settings.llm.__class__(
        default=None,
        agents={"market": ModelSpec("openai", "gpt-test", "OPENAI_API_KEY"),
                "portfolio_manager": ModelSpec("anthropic", "claude-test", "ANTHROPIC_API_KEY")})
    theme = _theme("industries", industries=("半導體業",), min_weight=0.3)
    ctx.settings = ctx.settings.__class__(**{**ctx.settings.__dict__, "llm": llm, "theme": theme})

    result = execute(ctx, dp_path, CAL, write_out=False)
    assert result.ok, errors(result)
    doc = json.loads(result.dplan_path.read_text(encoding="utf-8"))
    meta = doc["agent_metadata"]
    assert meta["model_provider"] == "openai"  # 只有市場分析師成功用 LLM（PM 降級為規則版）
    assert meta["model_version"] == "pm:rule;mk:gpt-test;rs:rule;rv:rule;in:rule"
    assert meta["input_tokens"] > 0 and meta["output_tokens"] > 0
    saved = json.loads((result.run_dir / "models.json").read_text(encoding="utf-8"))
    assert saved["per_agent"]["mk"] == "openai:gpt-test" and saved["per_agent"]["pm"] == "rule"
    assert (result.run_dir / "theme.json").exists()

    semis = {c for c, i in universe_industries().items() if i == "半導體業"}
    held = {d["ticker"]: d["target_weight"] for d in doc["decisions"]}
    assert sum(w for c, w in held.items() if c in semis) >= 0.3
