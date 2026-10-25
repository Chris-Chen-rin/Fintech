"""M6 Agent 之間的資料契約。

Agent 一律使用暫時代號：事實 F1、F2…（程式產生）、推論用字串代號（例如 T:2330）。
組裝 D-Plan 時才統一換成 S/O/I/D 編號；文字中寫 {F3} 會被換成對應的 O 編號。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from dplan_checker import rules as R

REGIMES = ("risk_on", "neutral", "risk_off")
STANCES = ("aggressive", "neutral", "defensive")
INTENTS = ("increase", "hold", "reduce")
VIEWS = ("positive", "neutral", "negative")
ACTIONS = ("BUY", "ADD", "TRIM", "SELL_ALL")
FACT_TOKEN = re.compile(r"\{(F\d+)\}")


class ContractError(Exception):
    def __init__(self, agent: str, problems: list[str]):
        super().__init__(f"{agent} 的輸出不符契約：" + "；".join(problems))
        self.agent = agent
        self.problems = problems


@dataclass
class Fact:
    """L1 事實：只放可核對的事實與數字。"""
    key: str
    sources: list[str]
    statement: str
    values: dict[str, float]
    topic: str
    scope: str  # "market" 或股票代號


@dataclass
class MarketView:
    basis: list[str]
    logic: str
    regime: str
    stance: str
    intent: str
    cash_range: tuple[float, float]
    cash_target: float
    counter_evidence: str | None
    confidence: float | None = None


@dataclass
class Thesis:
    """L2 推論。tickers 為空表示非個股推論。"""
    key: str
    tickers: list[str]
    premises: list[str]
    logic: str
    conclusion: str
    counter_evidence: str | None
    view: str = "neutral"
    score: float | None = None
    confidence: float | None = None
    author: str = ""


@dataclass
class Review:
    key: str
    verdict: str  # ok / weak
    note: str | None = None


@dataclass
class Decision:
    ticker: str
    action: str
    target_weight: float
    theses: list[str]
    risk_check: str | None = None
    funding_for: list[str] = field(default_factory=list)  # 被支應的買進股票代號


@dataclass
class NoTrade:
    ticker: str
    theses: list[str]
    reason: str | None = None


@dataclass
class PortfolioPlan:
    decisions: list[Decision]
    no_trades: list[NoTrade]
    theses: list[Thesis] = field(default_factory=list)  # 投資組合經理自己寫的推論（例如資金調度）
    notes: dict[str, Any] = field(default_factory=dict)


def _check_text(text: Any, name: str, lo: int, hi: int, problems: list[str]) -> None:
    if not isinstance(text, str) or not lo <= len(text) <= hi:
        problems.append(f"{name} 長度須介於 {lo}–{hi} 字")


def validate_facts(agent: str, facts: list[Fact], source_keys: set[str]) -> None:
    problems: list[str] = []
    for f in facts:
        if not f.sources or any(s not in source_keys for s in f.sources):
            problems.append(f"{f.key} 引用了不存在的來源")
        if len(f.sources) > 10:
            problems.append(f"{f.key} 引用來源超過 10 筆")
        _check_text(f.statement, f"{f.key}.statement", 5, 500, problems)
        if not f.values or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in f.values.values()):
            problems.append(f"{f.key}.values 至少一個數字且只能放數字")
        bad = [w for w in R.INFERENCE_WORDS if w in f.statement]
        if bad:
            problems.append(f"{f.key} 的事實含推論用語：{'、'.join(bad)}")
    if problems:
        raise ContractError(agent, problems)


def validate_market_view(agent: str, mv: MarketView, fact_keys: set[str]) -> None:
    problems: list[str] = []
    if not 1 <= len(mv.basis) <= 10 or any(k not in fact_keys for k in mv.basis):
        problems.append("basis 須為 1–10 筆存在的事實")
    _check_text(mv.logic, "logic", 20, 1000, problems)
    if mv.regime not in REGIMES:
        problems.append(f"regime {mv.regime!r} 不合法")
    if mv.stance not in STANCES:
        problems.append(f"stance {mv.stance!r} 不合法")
    if mv.intent not in INTENTS:
        problems.append(f"intent {mv.intent!r} 不合法")
    lo, hi = mv.cash_range
    if not 0 <= lo <= mv.cash_target <= hi <= R.CASH_MAX:
        problems.append(f"現金區間 [{lo}, {hi}] 與目標 {mv.cash_target} 不合理")
    if mv.counter_evidence == "":
        problems.append("counter_evidence 沒有就填 None")
    problems += _token_problems(mv.logic, set(mv.basis), "logic")
    if problems:
        raise ContractError(agent, problems)


def validate_theses(agent: str, theses: list[Thesis], fact_keys: set[str], universe: set[str]) -> None:
    problems: list[str] = []
    keys = [t.key for t in theses]
    if len(keys) != len(set(keys)):
        problems.append("推論代號重複")
    for t in theses:
        if not 1 <= len(t.premises) <= 10 or any(k not in fact_keys for k in t.premises):
            problems.append(f"{t.key} 的前提須為 1–10 筆存在的事實")
        _check_text(t.logic, f"{t.key}.logic", 20, 1000, problems)
        if len(t.conclusion) > 300:
            problems.append(f"{t.key}.conclusion 超過 300 字")
        if t.counter_evidence is not None and not 0 < len(t.counter_evidence) <= 500:
            problems.append(f"{t.key}.counter_evidence 須為 1–500 字或 None")
        if t.view not in VIEWS:
            problems.append(f"{t.key}.view {t.view!r} 不合法")
        if any(c not in universe for c in t.tickers):
            problems.append(f"{t.key} 提到不在 150 檔內的股票")
        problems += _token_problems(t.logic, set(t.premises), f"{t.key}.logic")
    if problems:
        raise ContractError(agent, problems)


def validate_plan(agent: str, plan: PortfolioPlan, thesis_keys: set[str], universe: set[str], held: set[str]) -> None:
    problems: list[str] = []
    keys = thesis_keys | {t.key for t in plan.theses}
    seen: set[str] = set()
    for d in plan.decisions:
        if d.ticker in seen:
            problems.append(f"{d.ticker} 重複出現")
        seen.add(d.ticker)
        if d.ticker not in universe:
            problems.append(f"{d.ticker} 不在 150 檔內")
        if d.action not in ACTIONS:
            problems.append(f"{d.ticker} 的動作 {d.action!r} 不合法")
        if not 0 <= d.target_weight <= R.TSMC_WEIGHT_CAP:
            problems.append(f"{d.ticker} 目標權重 {d.target_weight} 超出 0–0.25")
        if not 1 <= len(d.theses) <= 8 or any(k not in keys for k in d.theses):
            problems.append(f"{d.ticker} 須引用 1–8 筆存在的推論")
        if d.funding_for and d.action not in R.SELL_ACTIONS:
            problems.append(f"{d.ticker} 不是賣出卻標記 funding_for")
        if d.risk_check and len(d.risk_check) > 300:
            problems.append(f"{d.ticker}.risk_check 超過 300 字")
    for n in plan.no_trades:
        if n.ticker in seen:
            problems.append(f"{n.ticker} 重複出現")
        seen.add(n.ticker)
        if n.ticker not in held:
            problems.append(f"{n.ticker} 昨日沒有持股，不能列為續抱")
        if not 1 <= len(n.theses) <= 5 or any(k not in keys for k in n.theses):
            problems.append(f"{n.ticker} 續抱須引用 1–5 筆存在的推論")
        if n.reason and len(n.reason) > 300:
            problems.append(f"{n.ticker}.reason 超過 300 字")
    for code in sorted(held - seen):
        problems.append(f"昨日持股 {code} 沒有決策也沒有續抱")
    buy_tickers = {d.ticker for d in plan.decisions if d.action in R.BUY_ACTIONS}
    for d in plan.decisions:
        if any(t not in buy_tickers for t in d.funding_for):
            problems.append(f"{d.ticker} 的 funding_for 指向的不是同日買進")
    if len(plan.decisions) > 60 or len(plan.no_trades) > 30:
        problems.append("決策最多 60 筆、續抱最多 30 筆")
    if problems:
        raise ContractError(agent, problems)


def _token_problems(text: str, allowed: set[str], name: str) -> list[str]:
    bad = [t for t in FACT_TOKEN.findall(text or "") if t not in allowed]
    return [f"{name} 提到 {', '.join(bad)}，但沒有列在引用中"] if bad else []
