"""矛盾訊號偵測與反方審查。"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from agent.agents.reviewer import RuleReviewerAgent, conflict_notes
from agent.framework.contracts import Thesis
from agent.tools.fundamentals import Fundamentals
from agent.tools.quant import StockMetrics
from agent.tools.signals import conflicts, unaddressed


def chuanhu() -> StockMetrics:
    """仿 10/22 的川湖：營收年增 196%、較上月 -31%；前波漲 103% 後自高點回落 33%。"""
    f = Fundamentals("2059", "twse", rev_month="11509", rev_yoy=196.22, rev_mom=-30.95, rev_prev_month="11508",
                     rev_prev_yoy=344.41, rev_prev_mom=0.91, pe=53.78)
    return StockMetrics("2059", "川湖", "twse", 9755.0, -3.22, 0.36, -23.79, 5e9, 0.03, 17.89, fact="F1", fund=f,
                        high=(dt.date(2026, 8, 26), 14510.0), low=(dt.date(2026, 7, 30), 7140.0),
                        from_high=-32.77, low_to_high=103.22)


def thesis(logic: str, counter: str | None = None, view: str = "positive") -> Thesis:
    return Thesis("T:2059", ["2059"], ["F1"], logic, "川湖 正向", counter, view, 1.0, 0.7, "research/llm")


def test_conflicts_chuanhu():
    found = {c.code: c for c in conflicts(chuanhu())}
    assert set(found) == {"revenue_mom_drop", "pullback_after_rally"}
    assert "-30.95%" in found["revenue_mom_drop"].text and "+103.22%" in found["pullback_after_rally"].text


def test_unaddressed_by_keywords():
    items = conflicts(chuanhu())
    naive = thesis("營收年增 196.22%，基本面亮眼，但 20 日跌 23.79%，評價壓力大。")
    assert {c.code for c in unaddressed(naive, items)} == {"revenue_mom_drop", "pullback_after_rally"}
    explained = thesis("9 月營收較上月減少 30.95%，成長放緩；前波漲多後自高點回落，屬獲利了結。")
    assert unaddressed(explained, items) == []


def test_rule_reviewer_flags_unexplained_conflicts():
    ctx = SimpleNamespace(metrics={"2059": chuanhu()})
    t = thesis("營收年增 196.22%，基本面亮眼。")
    review = RuleReviewerAgent().run(ctx, {"theses": [t]})[0]
    assert review.verdict == "weak" and "較上月" in review.note
    assert conflict_notes(ctx, thesis("同上", view="neutral")) == []
