"""M10、M12 每日執行：資料包 → 行情 → 五個 Agent → 風控閘門 → 委託 → D-Plan → 檢查器 → 輸出。"""
from __future__ import annotations

import datetime as dt
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dplan_checker import rules as R
from dplan_checker.checker import CheckOptions, check_dplan
from dplan_checker.report import Report

from .agents import build_agents
from .datapack import DataPack, find_datapack, first_day_datapack, load_datapack, parse_datapack
from .dplan import assemble, filename
from .framework.context import FactBook, RunContext
from .framework.contracts import (
    PortfolioPlan,
    Review,
    validate_facts,
    validate_market_view,
    validate_plan,
    validate_theses,
)
from .framework.runner import AgentFailure, AgentRunner
from .llm.metadata import model_metadata
from .risk_gate import GateResult, check_plan, overweight_streaks
from .settings import Settings, code_version
from .sources import SourceRegistry
from .theme import resolve_theme
from .tools.fundamentals import FundamentalsLoader
from .tools.http import HttpClient
from .tools.market_data import PriceStore, load_history
from .tools.quant import build_market_facts, build_stock_facts, build_stock_metrics
from .trading_calendar import TradingCalendar, iso_tpe, now_tpe, to_tpe

PM_ATTEMPTS = 3        # 風控閘門退回投資組合經理的次數上限
CHECKER_RETRIES = 2    # 檢查器退回的次數上限
PM_CHECK_CODES = ("POSTURE", "CASH", "HOLDINGS", "WEIGHT", "DECISION", "ORDER", "ACTION", "FUNDING", "BUY_ZERO",
                  "PASSIVE", "COVERAGE", "NO_TRADE", "TRIM", "SELL_ALL", "DERIVATION", "OVERSELL")


class RunError(Exception):
    pass


class GateFailure(AgentFailure):
    def __init__(self, message: str, gate: GateResult):
        super().__init__(message)
        self.gate = gate


@dataclass
class RunOptions:
    datapack_path: Path | None = None
    mock_first_day: bool = False
    now: dt.datetime | None = None
    offline: bool = False
    history_days: int = 61  # 60 個交易日報酬（約 3 個月相對強度）
    write_out: bool = True


@dataclass
class RunResult:
    ok: bool
    run_dir: Path
    dplan_path: Path | None = None
    out_path: Path | None = None
    report: Report | None = None
    gate: GateResult | None = None
    agents_used: dict[str, str] = field(default_factory=dict)


def _universe_markets() -> tuple[dict[str, str], dict[str, str]]:
    data = json.loads(R.DEFAULT_UNIVERSE.read_text(encoding="utf-8"))
    return ({s["code"]: s["name"] for s in data["stocks"]}, {s["code"]: s["market"] for s in data["stocks"]})


def _load_datapack(settings: Settings, opts: RunOptions, trade_date: dt.date, moment: dt.datetime) -> tuple[DataPack, dict[str, Any]]:
    if opts.mock_first_day:
        raw = first_day_datapack(settings.team_id, trade_date, iso_tpe(moment))
        return parse_datapack(raw, None, settings.team_id), raw
    path = opts.datapack_path or find_datapack(settings.paths.inbox, trade_date)
    dp = load_datapack(path, settings.team_id)
    return dp, dp.raw


def run_daily(settings: Settings, opts: RunOptions | None = None) -> RunResult:
    opts = opts or RunOptions()
    real_now = now_tpe()
    moment = to_tpe(opts.now) if opts.now else real_now
    cal = TradingCalendar(settings.holidays)
    window = cal.target_window(moment)
    settings.paths.ensure()

    datapack, raw_dp = _load_datapack(settings, opts, window.trade_date, moment)
    if datapack.trade_date != window.trade_date:
        if not cal.is_trading_day(datapack.trade_date):
            raise RunError(f"資料包的 trade_date {datapack.trade_date} 不是交易日")
        print(f"⚠ 資料包對應 {datapack.trade_date}，與依時間推算的 {window.trade_date} 不同；以資料包為準")
        window = cal.window(datapack.trade_date)

    session = window.session(moment)
    run_dir = settings.paths.archive_for(window.trade_date) / f"{moment:%Y%m%d_%H%M%S}_{session}"
    run_dir.mkdir(parents=True, exist_ok=True)
    dp_path = run_dir / "datapack.json"
    dp_path.write_text(json.dumps(raw_dp, ensure_ascii=False, indent=2), encoding="utf-8")

    universe, markets = _universe_markets()
    client = HttpClient(settings.paths.cache, offline=opts.offline)
    store = PriceStore(client, settings.paths.cache, universe, markets)
    print(f"▶ {window.describe(moment)}")
    print(f"▶ 抓取行情：截至 {window.prev_trading_date} 的 {opts.history_days} 個交易日（已抓過的日子直接用快取）")
    history = load_history(store, window.prev_trading_date, opts.history_days, cal.is_trading_day)

    prices = history.closes(history.last)
    for code, p in datapack.close_prices().items():
        ext = prices.get(code)
        if ext is not None and abs(ext - p) / p > 0.001:
            print(f"⚠ {code} 收盤價：資料包 {p}、交易所 {ext}，以資料包為準")
        prices[code] = p

    print("▶ 抓取財報：公開資訊觀測站財報彙總、月營收、本益比／股價淨值比")
    fundamentals: dict[str, Any] = {}
    fund_notes: list[str] = []
    try:
        loader = FundamentalsLoader(client, settings.paths.cache, markets, window.prev_trading_date)
        fundamentals = loader.load(history.closes(history.last))
        fund_notes = loader.notes
    except Exception as exc:  # noqa: BLE001 - 財報是加分資料，失敗時照常以價量交件
        fund_notes = [f"財報資料載入失敗，本次只用價量：{exc}"]

    ctx = RunContext(settings=settings, window=window, datapack=datapack, universe=universe, markets=markets,
                     history=history, prices=prices, registry=SourceRegistry(), facts=FactBook(), run_dir=run_dir,
                     started_at=moment, clock_offset=moment - real_now, fundamentals=fundamentals)
    ctx.log(f"▶ 行情 {len(history.dates)} 個交易日（{history.dates[0]} ～ {history.last}），本次連線 {client.request_count} 次")
    with_fs = sum(1 for f in fundamentals.values() if f.season)
    ctx.log(f"▶ 財報：{with_fs}/{len(universe)} 檔有最近一季財報" + "".join(f"；⚠ {n}" for n in fund_notes))
    return execute(ctx, dp_path, cal, opts.write_out)


def execute(ctx: RunContext, dp_path: Path, cal: TradingCalendar, write_out: bool = True) -> RunResult:
    """資料備齊後的流程：量化事實 → 五個 Agent → 風控閘門 → 組裝 → 檢查器 → 輸出。"""
    settings, datapack, window, run_dir, moment = ctx.settings, ctx.datapack, ctx.window, ctx.run_dir, ctx.started_at
    universe = ctx.universe
    ctx.log(f"▶ 資料包：淨值 {datapack.book.nav:,.0f}、現金比 {datapack.cash_ratio:.1%}、持股 {len(ctx.held)} 檔"
            + ("（首日）" if datapack.is_first_day else ""))
    ctx.metrics = build_stock_metrics(ctx)
    ctx.theme = resolve_theme(settings.theme, ctx.metrics, ctx.held)
    ctx.save("theme_membership.json", ctx.theme)
    ctx.log(f"▶ 主題（{ctx.theme.mode}）：成員 {len(ctx.theme.members)} 檔" + "".join(f"；{n}" for n in ctx.theme.notes))
    market_keys = build_market_facts(ctx)
    build_stock_facts(ctx)
    ctx.log(f"▶ 量化事實：市場 {len(market_keys)} 筆、個股價量 {len(ctx.metrics)} 筆、"
            f"財報 {sum(1 for m in ctx.metrics.values() if m.fund_fact)} 筆")

    agents = build_agents(settings)
    runner = AgentRunner(ctx)
    source_keys = {r.key for r in ctx.registry.all()}

    intel_facts = runner.run("intel", agents["intel"], {"trade_date": window.trade_date.isoformat()},
                             lambda n, o: validate_facts(n, o, source_keys))
    ctx.facts.extend(intel_facts)

    market_inputs = {"market_facts": ctx.facts.of("market"), "market_metrics": ctx.market_metrics,
                     "cash_ratio": datapack.cash_ratio, "first_day": datapack.is_first_day}
    mv = runner.run("market", agents["market"], market_inputs, lambda n, o: validate_market_view(n, o, ctx.facts.keys()))
    ctx.log(f"▶ 市場策略：{mv.regime}／{mv.stance}／{mv.intent}，現金區間 {mv.cash_range[0]:.0%}–{mv.cash_range[1]:.0%}")

    taiex_fact = next((f.key for f in ctx.facts.of("market") if "TAIEX_ret_20d_pct" in f.values), None)
    research_inputs: dict[str, Any] = {"candidates": sorted(ctx.metrics), "taiex_trend_fact": taiex_fact, "weak": []}
    universe_set = set(universe)
    check_theses = lambda n, o: validate_theses(n, o, ctx.facts.keys(), universe_set)  # noqa: E731
    theses = runner.run("research", agents["research"], research_inputs, check_theses)

    reviews: list[Review] = runner.run("reviewer", agents["reviewer"], {"theses": theses},
                                       lambda n, o: _validate_reviews(n, o, {t.key for t in theses}))
    weak = [r for r in reviews if r.verdict == "weak"]
    if weak:
        research_inputs["weak"] = [t.tickers[0] for t in theses if t.key in {r.key for r in weak} and t.tickers]
        theses = runner.run("research_retry", agents["research"], research_inputs, check_theses,
                            feedback=[f"{r.key}：{r.note}" for r in weak])
        notes = {r.key: r.note for r in weak}
        for t in theses:
            if t.key in notes and notes[t.key] and notes[t.key] not in (t.counter_evidence or ""):
                t.counter_evidence = ((t.counter_evidence + "；") if t.counter_evidence else "") + f"反方審查：{notes[t.key]}"
    thesis_map = {t.key: t for t in theses}
    ctx.log(f"▶ 研究：{len(theses)} 筆推論（正向 {sum(t.view == 'positive' for t in theses)}、"
            f"負向 {sum(t.view == 'negative' for t in theses)}）；反方審查退回 {len(weak)} 筆")

    streaks = overweight_streaks(datapack, cal.prev_trading_day)
    pm_inputs = {"market_view": mv, "theses": theses, "first_day": datapack.is_first_day, "overweight_streaks": streaks}
    try:
        plan, gate = _run_pm(ctx, runner, agents, pm_inputs, mv, thesis_map, streaks, [])
    except GateFailure as exc:
        s = exc.gate.summary
        ctx.log(f"  ↺ 投資組合經理無法達成市場策略的姿態，退回市場策略 Agent 重新決定"
                f"（淨買超 {s.get('net_flow_pct')}%、現金比 {s.get('cash_ratio_gross')}）")
        market_inputs["portfolio_feedback"] = {"net_flow_pct": s.get("net_flow_pct"), "cash_ratio": s.get("cash_ratio_gross")}
        mv = runner.run("market_retry", agents["market"], market_inputs,
                        lambda n, o: validate_market_view(n, o, ctx.facts.keys()), feedback=[str(exc)])
        ctx.log(f"▶ 市場策略（修正）：{mv.regime}／{mv.stance}／{mv.intent}，現金區間 {mv.cash_range[0]:.0%}–{mv.cash_range[1]:.0%}")
        pm_inputs["market_view"] = mv
        plan, gate = _run_pm(ctx, runner, agents, pm_inputs, mv, thesis_map, streaks, [])

    result = RunResult(False, run_dir, gate=gate, agents_used=runner.used)
    for attempt in range(CHECKER_RETRIES + 1):
        doc = assemble(ctx, mv, thesis_map, plan, gate.orders, _metadata(ctx, runner, moment))
        dplan_path = run_dir / filename(settings.team_id, window.trade_date.isoformat())
        dplan_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        report = _check(ctx, dplan_path, dp_path)
        result.report, result.dplan_path = report, dplan_path
        if report.ok:
            break
        pm_errors = [f"{i.code}：{i.message}" for i in report.errors if i.code.startswith(PM_CHECK_CODES)]
        if attempt == CHECKER_RETRIES or not pm_errors:
            break
        ctx.log(f"  ↺ 檢查器發現 {len(pm_errors)} 個決策相關錯誤，退回投資組合經理（第 {attempt + 1} 次）")
        plan, gate = _run_pm(ctx, runner, agents, pm_inputs, mv, thesis_map, streaks, pm_errors)
        result.gate = gate

    ctx.save("gate.json", {"violations": [str(v) for v in gate.violations], "summary": gate.summary})
    (run_dir / "check_report.txt").write_text(result.report.format_text(), encoding="utf-8")
    ctx.save("check_report.json", result.report.to_dict())
    result.ok = result.report.ok
    if result.ok and write_out:
        out = settings.paths.out / result.dplan_path.name
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(result.dplan_path, out)
        result.out_path = out
    (run_dir / "run.log").write_text("\n".join(ctx.log_lines), encoding="utf-8")
    return result


def _validate_reviews(agent: str, reviews: list[Review], keys: set[str]) -> None:
    from .framework.contracts import ContractError
    bad = [r.key for r in reviews if r.key not in keys or r.verdict not in ("ok", "weak")]
    if bad:
        raise ContractError(agent, [f"審查結果引用不存在的推論或結論不合法：{bad[:5]}"])


def _run_pm(ctx: RunContext, runner: AgentRunner, agents: dict[str, list], inputs: dict[str, Any], mv, thesis_map,
            streaks: dict[str, int], feedback: list[str]) -> tuple[PortfolioPlan, GateResult]:
    universe_set = set(ctx.universe)
    for attempt in range(1, PM_ATTEMPTS + 1):
        plan = runner.run("portfolio_manager", agents["portfolio_manager"], inputs,
                          lambda n, o: validate_plan(n, o, set(thesis_map), universe_set, ctx.held), feedback=feedback)
        gate = check_plan(plan, mv, ctx.datapack, ctx.prices, ctx.settings, streaks,
                          ctx.theme.members if ctx.theme else None)
        ctx.save(f"gate_attempt{attempt}.json", {"violations": [str(v) for v in gate.violations], "summary": gate.summary})
        if gate.ok:
            s = gate.summary
            ctx.log(f"▶ 投資組合經理：{len(plan.decisions)} 筆決策、{len(plan.no_trades)} 筆續抱；委託 {s['orders']} 筆，"
                    f"交易後持股 {s['holdings_after']} 檔、現金比 {s['cash_ratio_gross']:.2%}、淨買超 {s['net_flow_pct']:+.2f}% 淨值")
            for w in gate.violations:
                ctx.log(f"  ⚠ 風控提醒 {w}")
            return plan, gate
        feedback = feedback + [str(v) for v in gate.errors]
        ctx.log(f"  ↺ 風控閘門退回（第 {attempt} 次）：" + "；".join(str(v) for v in gate.errors[:3]))
    raise GateFailure("投資組合經理連續 3 次未通過風控閘門：" + "；".join(feedback[-3:]), gate)


def _metadata(ctx: RunContext, runner: AgentRunner, moment: dt.datetime) -> dict[str, Any]:
    all_rule = all(v == "rule" for v in runner.used.values())
    provider, version, mapping = model_metadata(runner.used, runner.models)
    ctx.save("models.json", {"model_provider": provider, "model_version": version, "per_agent": mapping,
                             "per_step": {s: (f"{runner.models[s][0]}:{runner.models[s][1]}" if v == "llm" else "rule")
                                          for s, v in runner.used.items()}})
    meta: dict[str, Any] = {
        "model_provider": provider,
        "model_version": version,
        "run_started_at": iso_tpe(moment),
        "run_completed_at": iso_tpe(ctx.now()),
        "code_version": code_version(),
    }
    if not all_rule:
        meta["input_tokens"] = ctx.usage["input_tokens"]
        meta["output_tokens"] = ctx.usage["output_tokens"]
    meta["tool_use_count"] = ctx.usage["tool_use_count"] + len(runner.used)
    return meta


def _check(ctx: RunContext, dplan_path: Path, dp_path: Path) -> Report:
    prices_path = ctx.run_dir / "prev_close.json"
    prices_path.write_text(json.dumps(ctx.prices, ensure_ascii=False), encoding="utf-8")
    theme_path = None
    theme = ctx.settings.theme
    if ctx.theme and ctx.theme.active:
        theme_path = ctx.run_dir / "theme.json"
        theme_path.write_text(json.dumps({"name": theme.name or "主題", "tickers": sorted(ctx.theme.members),
                                          "min_weight": theme.min_weight}, ensure_ascii=False), encoding="utf-8")
    opts = CheckOptions(datapack_path=dp_path, prices_path=prices_path, theme_path=theme_path,
                        team_id=ctx.settings.team_id, now=ctx.now(), holidays=R.KNOWN_HOLIDAYS | ctx.settings.holidays)
    return check_dplan(dplan_path, opts)
