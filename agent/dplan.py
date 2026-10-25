"""M9 D-Plan 組裝：各 Agent 的暫時代號 → 連續的 S/O/I/D 編號、引用鏈、agent_metadata。

只收錄實際被引用的推論、事實與來源，避免孤兒項目；文字中的 {F3} 換成對應的 O 編號。
"""
from __future__ import annotations

from typing import Any

from dplan_checker import rules as R

from .framework.context import RunContext
from .framework.contracts import FACT_TOKEN, MarketView, PortfolioPlan, Thesis
from .orders import DerivedOrder

LIMITS = {"sources": 50, "observations": 100, "inferences": 60, "decisions": 60, "no_trade_decisions": 30}


class AssemblyError(Exception):
    pass


def _unique(items: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for x in items:
        seen.setdefault(x, None)
    return list(seen)


def assemble(ctx: RunContext, mv: MarketView, theses: dict[str, Thesis], plan: PortfolioPlan,
             orders: list[DerivedOrder], metadata: dict[str, Any]) -> dict[str, Any]:
    all_theses = dict(theses)
    all_theses.update({t.key: t for t in plan.theses})

    thesis_keys = _unique([k for d in plan.decisions for k in d.theses] + [k for n in plan.no_trades for k in n.theses])
    missing = [k for k in thesis_keys if k not in all_theses]
    if missing:
        raise AssemblyError(f"決策引用了不存在的推論：{missing}")
    fact_keys = _unique(list(mv.basis) + [p for k in thesis_keys for p in all_theses[k].premises])
    facts = [ctx.facts.get(k) for k in fact_keys]
    source_keys = _unique([s for f in facts for s in f.sources])

    s_id = {k: f"S{i}" for i, k in enumerate(source_keys, 1)}
    o_id = {k: f"O{i}" for i, k in enumerate(fact_keys, 1)}
    i_id = {k: f"I{i}" for i, k in enumerate(thesis_keys, 1)}
    d_id = {d.ticker: f"D{i}" for i, d in enumerate(plan.decisions, 1)}

    def text(s: str | None) -> str | None:
        return FACT_TOKEN.sub(lambda m: o_id.get(m.group(1), m.group(1)), s) if s is not None else None

    sources = []
    for k in source_keys:
        src = ctx.registry.get(k).to_dplan(s_id[k])
        if src.get("fetched_at", "") > metadata["run_completed_at"]:
            src.pop("fetched_at")
        sources.append(src)

    doc: dict[str, Any] = {
        "schema_version": R.SCHEMA_VERSION,
        "doc_type": "D-Plan",
        "team_id": ctx.settings.team_id,
        "trade_date": ctx.datapack.trade_date.isoformat(),
        "sources": sources,
        "observations": [
            {"obs_id": o_id[f.key], "source_ref": [s_id[s] for s in _unique(f.sources)], "topic": f.topic,
             "statement": f.statement, "values": f.values}
            for f in facts
        ],
        "market_view": {
            "basis_refs": [o_id[k] for k in mv.basis],
            "logic": text(mv.logic),
            "regime": mv.regime,
            "stance": mv.stance,
            "posture": {"net_exposure_intent": mv.intent, "target_cash_pct_range": [mv.cash_range[0], mv.cash_range[1]]},
            "counter_evidence": text(mv.counter_evidence),
        },
        "inferences": [],
        "decisions": [],
        "no_trade_decisions": [],
        "orders": [],
        "agent_metadata": metadata,
    }
    if mv.confidence is not None:
        doc["market_view"]["confidence"] = mv.confidence

    for k in thesis_keys:
        t = all_theses[k]
        item: dict[str, Any] = {"inf_id": i_id[k], "premise_refs": [o_id[p] for p in _unique(t.premises)], "logic": text(t.logic)}
        if t.conclusion:
            item["conclusion"] = text(t.conclusion)
        item["counter_evidence"] = text(t.counter_evidence)
        if t.confidence is not None:
            item["confidence"] = t.confidence
        doc["inferences"].append(item)

    for d in plan.decisions:
        item = {"decision_id": d_id[d.ticker], "ticker": d.ticker, "action": d.action, "target_weight": d.target_weight,
                "inference_refs": [i_id[k] for k in _unique(d.theses)]}
        if d.risk_check:
            item["risk_check"] = d.risk_check
        if d.funding_for:
            item["funding_for"] = [d_id[t] for t in _unique(d.funding_for)]
        doc["decisions"].append(item)

    for n in plan.no_trades:
        item = {"ticker": n.ticker, "reason_refs": [i_id[k] for k in _unique(n.theses)]}
        if n.reason:
            item["reason"] = n.reason
        doc["no_trade_decisions"].append(item)

    for o in orders:
        doc["orders"].append({"ticker": o.ticker, "side": o.side, "shares": o.shares,
                              "decision_ref": d_id[plan.decisions[o.decision_index].ticker], "derivation_detail": o.detail})

    for key, limit in LIMITS.items():
        if len(doc[key]) > limit:
            raise AssemblyError(f"{key} 有 {len(doc[key])} 筆，超過規格上限 {limit}")
    return doc


def filename(team_id: str, trade_date: str) -> str:
    return f"D-Plan_{team_id}_{trade_date}.json"
