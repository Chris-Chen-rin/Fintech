"""A2 市場策略 Agent：市場級事實 → market_view（走向、姿態、現金區間、淨曝險意圖）。"""
from __future__ import annotations

from typing import Any

from ..framework.context import RunContext
from ..framework.contracts import MarketView

REGIME_TO_STANCE = {"risk_on": "aggressive", "neutral": "neutral", "risk_off": "defensive"}
REGIME_LABEL = {"risk_on": "偏多（risk_on）", "neutral": "中性（neutral）", "risk_off": "偏空（risk_off）"}
ENTER, EXIT = 0.8, 0.3  # 進入偏多／偏空要 |分數| ≥ 0.8；離開要回到 |分數| < 0.3（遲滯，避免天天翻轉）


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


class RuleMarketAgent:
    """依加權指數 1／5／20 日變動與 150 檔漲跌家數打分；參考前一交易日的走向判斷（遲滯）。"""
    name = "market"
    variant = "rule"

    def run(self, ctx: RunContext, inputs: dict[str, Any], feedback: list[str] | None = None) -> MarketView:
        mm = inputs["market_metrics"]
        facts = inputs["market_facts"]
        risk = ctx.settings.risk
        r1, r5, r20 = mm.get("taiex_chg_pct"), mm.get("taiex_ret_5d"), mm.get("taiex_ret_20d")
        breadth = mm.get("breadth", 0.5)

        score, signals_up, signals_down = 0.0, [], []
        if r20 is not None:
            score += _clip(r20 / 5)
            (signals_up if r20 > 0 else signals_down).append(f"加權指數 20 日 {r20:+.2f}%")
        if r5 is not None:
            score += 0.7 * _clip(r5 / 3)
            (signals_up if r5 > 0 else signals_down).append(f"5 日 {r5:+.2f}%")
        score += (breadth - 0.5) * 2 * 0.5
        (signals_up if breadth >= 0.5 else signals_down).append(f"150 檔上漲家數占 {breadth:.0%}")
        if r1 is not None and r1 <= -2.5:
            score -= 1.0
            signals_down.append(f"前一交易日大跌 {r1:+.2f}%")
        prev = inputs.get("prev_regime")
        regime = "risk_on" if score >= ENTER else "risk_off" if score <= -ENTER else "neutral"
        sticky = ""
        if prev == "risk_off" and regime == "neutral" and score < -EXIT:
            regime, sticky = "risk_off", f"前一交易日為偏空，分數未回升到 {-EXIT:+.1f} 以上，維持偏空以免來回進出"
        elif prev == "risk_on" and regime == "neutral" and score > EXIT:
            regime, sticky = "risk_on", f"前一交易日為偏多，分數未跌破 {EXIT:+.1f}，維持偏多以免來回進出"

        lo, hi = risk.cash_target_range
        target = {"risk_on": lo + 0.02, "neutral": risk.first_day_cash_target, "risk_off": hi - 0.03}[regime]
        target = round(min(max(target, lo + 0.01), hi - 0.01), 4)
        # 中性的上緣放寬到與偏空區間重疊：偏空後轉中性時保留現金，不立刻買回（轉偏多才回補）
        upper = target + (0.06 if regime == "neutral" else 0.04)
        cash_range = (round(max(lo, target - 0.03), 4), round(min(hi, upper), 4))

        current = inputs["cash_ratio"]
        if current > cash_range[1]:
            intent = "increase"
        elif current < cash_range[0]:
            intent = "reduce"
        else:
            intent = "hold"
        revision = ""
        fb = inputs.get("portfolio_feedback")
        if fb:
            # 投資組合經理回報做不到原姿態（例如強制減碼釋出大量現金）：依可行的部位重新宣告
            achievable, net = fb["cash_ratio"], fb["net_flow_pct"]
            target = round(min(max(achievable, lo), 0.18), 4)
            cash_range = (round(max(0.02, target - 0.03), 4), round(min(0.21, target + 0.03), 4))
            intent = "increase" if net > 1.5 else "reduce" if net < -1.5 else "hold"
            revision = (f"投資組合經理回報原姿態不可行（可行方案淨買超 {net:+.2f}% 淨值、現金比 {achievable:.1%}），"
                        f"改宣告目標現金比 {target:.0%}。")
        stance = REGIME_TO_STANCE[regime]
        if (stance, intent) in {("aggressive", "reduce"), ("defensive", "increase")}:
            stance = "neutral"

        refs = " ".join(f"{{{f.key}}}" for f in facts)
        intent_text = {"increase": "提高股票曝險（淨買超）", "hold": "總曝險大致持平", "reduce": "降低股票曝險（淨賣超）"}[intent]
        logic = (f"依據 {refs}：綜合分數 {score:+.2f}（20 日、5 日指數變動與上漲家數加權），"
                 f"研判市場{REGIME_LABEL[regime]}。目前現金比 {current:.1%}，目標現金比 {target:.0%}、"
                 f"區間 {cash_range[0]:.0%}–{cash_range[1]:.0%}，因此今日{intent_text}。")
        if sticky:
            logic += sticky + "。"
        if inputs.get("first_day"):
            logic += "今天是首個交易日，帳戶為全現金，需一次建立 20 檔以上的持股。"
        logic += revision
        opposite = signals_down if regime != "risk_off" else signals_up
        counter = ("反方訊號：" + "、".join(opposite)) if opposite else None
        return MarketView(
            basis=[f.key for f in facts][:10], logic=logic[:1000], regime=regime, stance=stance, intent=intent,
            cash_range=cash_range, cash_target=target, counter_evidence=counter,
            confidence=round(min(0.9, 0.5 + abs(score) / 4), 2),
        )
