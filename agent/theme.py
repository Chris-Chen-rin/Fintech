"""主題成員：依 theme.mode 把主題換成每檔 0–1 的相關度與成員名單。

- tickers：設定的代號清單
- industries：150 檔清單的產業別（來源：證交所、櫃買中心月營收資料）
- rule：量化指標條件；已持有的股票用較寬鬆的退出門檻
- llm：賽前由 LLM 依主題描述分類、存成版本化的檔案（{代號: 相關度}），賽中固定不變
- none：沒有主題成員，主題占比檢查略過
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from dplan_checker import rules as R

from .settings import ThemeRule, ThemeSettings

MEMBER_THRESHOLD = 0.5


@dataclass
class ThemeMembership:
    mode: str
    name: str
    scores: dict[str, float] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    misses: dict[str, str] = field(default_factory=dict)  # 非成員 → 未通過的條件
    notes: list[str] = field(default_factory=list)

    @property
    def members(self) -> set[str]:
        return {c for c, s in self.scores.items() if s >= MEMBER_THRESHOLD}

    @property
    def active(self) -> bool:
        return bool(self.members)

    def score(self, code: str) -> float:
        return self.scores.get(code, 0.0)


def universe_industries() -> dict[str, str]:
    data = json.loads(R.DEFAULT_UNIVERSE.read_text(encoding="utf-8"))
    return {s["code"]: s.get("industry", "") for s in data["stocks"]}


def metric_value(met: Any, name: str) -> float | None:
    """先找價量指標，再找財報指標（met.fund）。"""
    value = getattr(met, name, None)
    if value is None and getattr(met, "fund", None) is not None:
        value = getattr(met.fund, name, None)
    return value


def _passes(rule: ThemeRule, value: float | None, held: bool) -> bool:
    if value is None:
        return False
    lo = rule.held_min if held and rule.held_min is not None else rule.min
    hi = rule.held_max if held and rule.held_max is not None else rule.max
    return (lo is None or value >= lo) and (hi is None or value <= hi)


def resolve_theme(theme: ThemeSettings, metrics: dict[str, Any], held: set[str]) -> ThemeMembership:
    m = ThemeMembership(theme.mode, theme.name)
    if theme.mode == "tickers":
        m.scores = {t: 1.0 for t in theme.tickers}
        m.reasons = {t: "列於主題標的清單" for t in theme.tickers}
    elif theme.mode == "industries":
        wanted = set(theme.industries)
        for code, ind in universe_industries().items():
            if ind in wanted:
                m.scores[code] = 1.0
                m.reasons[code] = f"產業別為{ind}"
    elif theme.mode == "rule":
        for code, met in metrics.items():
            results = [_passes(r, metric_value(met, r.metric), code in held) for r in theme.rules]
            if results and all(results):
                m.scores[code] = 1.0
                m.reasons[code] = "符合主題條件：" + "、".join(_describe(r, metric_value(met, r.metric)) for r in theme.rules)
            else:
                m.misses[code] = "、".join(
                    (_describe(r, v) if (v := metric_value(met, r.metric)) is not None else f"{LABELS[r.metric][0]}無資料")
                    for r, ok in zip(theme.rules, results) if not ok)
        if theme.rules and not any(metric_value(met, r.metric) is not None for met in metrics.values() for r in theme.rules):
            m.notes.append("主題條件用到的指標全部缺值（財報資料未載入？）")
    elif theme.mode == "llm":
        path = theme.membership_file
        if path and path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for code, item in (data.get("scores") or data).items():
                if isinstance(item, dict):
                    m.scores[str(code)] = float(item.get("score", 0.0))
                    if item.get("reason"):
                        m.reasons[str(code)] = str(item["reason"])[:120]
                elif isinstance(item, (int, float)):
                    m.scores[str(code)] = float(item)
        else:
            m.notes.append("主題分類檔不存在，本次沒有主題成員")
    if theme.mode != "none" and not m.members:
        m.notes.append("主題成員為 0 檔，選股不會偏向主題")
    return m


LABELS = {
    "ret_5d": ("5 日報酬", "%"), "ret_20d": ("20 日報酬", "%"), "ret_60d": ("60 日報酬", "%"), "chg_pct": ("當日漲跌", "%"),
    "avg_value": ("20 日均成交值", " 元"), "volatility": ("日報酬波動", ""), "close": ("收盤價", " 元"),
    "market_cap": ("市值", " 元"), "market_cap_rank": ("市值排名第", " 名"), "ni_ttm": ("近四季歸屬母公司淨利", " 千元"),
    "eps_ttm": ("近四季 EPS", " 元"), "eps_growth": ("累計 EPS 年增", "%"), "roe": ("推算 ROE", "%"),
    "pe": ("本益比", " 倍"), "pb": ("股價淨值比", " 倍"), "rev_yoy": ("月營收年增", "%"),
    "rev_cum_yoy": ("累計營收年增", "%"), "cash_conversion": ("營業現金流／淨利", " 倍"), "dividend_yield": ("殖利率", "%"),
}


def _describe(rule: ThemeRule, value: float) -> str:
    label, unit = LABELS[rule.metric]
    if rule.metric == "ni_ttm":
        return f"{label} {value / 1e5:,.2f} 億元"
    if rule.metric in ("market_cap", "avg_value"):
        return f"{label} {value / 1e8:,.0f} 億元"
    text = f"{value:,.0f}" if rule.metric == "market_cap_rank" else f"{value:,.2f}"
    return f"{label} {text}{unit}"
