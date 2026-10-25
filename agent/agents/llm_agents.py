"""市場策略、研究、反方審查、投資組合經理的 LLM 版。

分工：量化工具算好所有數字（報酬、財報指標、規則分數、股數與權重），LLM 讀事實做判斷並寫理由；
程式只檢查 LLM 輸出是否符合契約（引用的事實存在、代號合法、檔數與比重限制），不改它的判斷。
不合格就把問題當作 feedback 重問一次，仍失敗才降級為規則版。
"""
from __future__ import annotations

import re
from typing import Any

from dplan_checker import rules as R

from ..framework.context import RunContext
from ..framework.contracts import FACT_TOKEN, INTENTS, REGIMES, STANCES, VIEWS, MarketView, Review, Thesis
from ..llm.base import LLMError
from ..settings import PROJECT_ROOT
from .llm_base import LLMAgent
from .portfolio_manager import RulePortfolioManagerAgent, anchor_codes
from .research import RuleResearchAgent
from .reviewer import conflict_notes
from ..tools.signals import conflicts

FOCUS = 40  # LLM 研究的檔數：規則分數前段＋現有持股＋有重大訊息的股票
NO_INFER = "文中提到的數字必須出自輸入的事實，不得捏造；全部用繁體中文。"
PLAYBOOK = PROJECT_ROOT / "config" / "research_playbook.md"


def playbook() -> str:
    """個股研究分析手冊（去掉標題與說明行）；檔案不存在時回傳空字串。"""
    if not PLAYBOOK.exists():
        return ""
    lines = [ln for ln in PLAYBOOK.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")
             and not ln.startswith("改這份")]
    return "分析原則：\n" + "\n".join(lines)


def _fact_line(ctx: RunContext, key: str) -> str:
    f = ctx.facts.get(key)
    return f"{{{key}}} {f.statement}"


def _strip_tokens(text: str, allowed: set[str]) -> str:
    return FACT_TOKEN.sub(lambda m: m.group(0) if m.group(1) in allowed else "", text)


def _str(data: dict, name: str, lo: int, hi: int) -> str:
    v = data.get(name)
    if not isinstance(v, str) or not lo <= len(v.strip()) <= hi:
        raise LLMError(f"{name} 必須是 {lo}–{hi} 字的文字")
    return v.strip()


def _float(data: dict, name: str, lo: float, hi: float) -> float:
    v = data.get(name)
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= float(v) <= hi:
        raise LLMError(f"{name} 必須是 {lo}–{hi} 的數字")
    return float(v)


# ---------------------------------------------------------------- 市場策略

class LLMMarketAgent(LLMAgent):
    name = "market"

    def build_prompt(self, ctx: RunContext, inputs: dict[str, Any], feedback: list[str] | None) -> tuple[str, str]:
        risk = ctx.settings.risk
        lo, hi = (0.02, 0.21) if inputs.get("portfolio_feedback") else risk.cash_target_range
        self._bounds = (lo, hi)
        system = (
            "你是台股主動式 ETF 的市場策略分析師，負責每天判斷整體市場並決定現金水位。"
            f"ETF 主題：{ctx.settings.theme.name}。{ctx.settings.theme.description}\n"
            "規則：regime 為 risk_on／neutral／risk_off；stance 為 aggressive／neutral／defensive；"
            "intent 為 increase（淨買超）／hold／reduce（淨賣超），要與目前現金比和你宣告的現金區間一致："
            "目前現金比高於區間上限就是 increase、低於下限就是 reduce、落在區間內是 hold。"
            "aggressive 不能配 reduce，defensive 不能配 increase。"
            f"現金區間與目標都要介於 {lo:.2f}–{hi:.2f}，區間寬度 0.04–0.10。"
            "若前一交易日有判斷，除非事實明顯反轉，否則維持原判斷，避免天天翻轉造成周轉成本。"
            "logic（80–600 字）要引用事實代號（寫成 {F1} 這種格式）說明判斷；counter_evidence 寫相反的訊號。"
            + NO_INFER +
            '\n輸出 JSON：{"regime":"…","stance":"…","intent":"…","cash_target":0.08,"cash_range":[0.05,0.12],'
            '"basis":["F1"],"logic":"…","counter_evidence":"…","confidence":0.6}'
        )
        facts = inputs["market_facts"]
        user = (f"交易日 {ctx.window.trade_date}。\n市場事實：\n" + "\n".join(f"{{{f.key}}} {f.statement}" for f in facts)
                + f"\n\n指標：{ {k: v for k, v in inputs['market_metrics'].items() if isinstance(v, (int, float))} }"
                + f"\n目前現金比 {inputs['cash_ratio']:.4f}"
                + ("\n今天是首個交易日，帳戶全現金，必須 increase 並建立 20 檔以上持股。" if inputs.get("first_day") else "")
                + (f"\n前一交易日判斷：{inputs['prev_regime']}" if inputs.get("prev_regime") else "")
                + (f"\n投資組合經理回報原姿態不可行：{inputs['portfolio_feedback']}，請依可行的現金比重新宣告。"
                   if inputs.get("portfolio_feedback") else ""))
        return system, user

    def parse(self, ctx: RunContext, inputs: dict[str, Any], data: Any) -> MarketView:
        if not isinstance(data, dict):
            raise LLMError("輸出必須是 JSON 物件")
        regime, stance, intent = data.get("regime"), data.get("stance"), data.get("intent")
        if regime not in REGIMES or stance not in STANCES or intent not in INTENTS:
            raise LLMError("regime／stance／intent 不在允許的值內")
        if (stance, intent) in {("aggressive", "reduce"), ("defensive", "increase")}:
            raise LLMError(f"stance {stance} 與 intent {intent} 互相矛盾")
        lo, hi = self._bounds
        rng = data.get("cash_range")
        if not (isinstance(rng, list) and len(rng) == 2 and all(isinstance(x, (int, float)) for x in rng)):
            raise LLMError("cash_range 必須是 [下限, 上限]")
        c_lo, c_hi = round(float(rng[0]), 4), round(float(rng[1]), 4)
        target = round(_float(data, "cash_target", lo, hi), 4)
        if not lo <= c_lo <= target <= c_hi <= hi or not 0.04 - 1e-9 <= c_hi - c_lo <= 0.10 + 1e-9:
            raise LLMError(f"現金區間 [{c_lo}, {c_hi}]、目標 {target} 須滿足 {lo} ≤ 下限 ≤ 目標 ≤ 上限 ≤ {hi}，寬度 0.04–0.10")
        cash = inputs["cash_ratio"]
        expect = "increase" if cash > c_hi else "reduce" if cash < c_lo else "hold"
        if inputs.get("first_day"):
            expect = "increase"
        if intent != expect:
            raise LLMError(f"目前現金比 {cash:.4f}、宣告區間 [{c_lo}, {c_hi}]，intent 應為 {expect}，不是 {intent}")
        keys = {f.key for f in inputs["market_facts"]}
        logic = _str(data, "logic", 40, 1000)
        basis = [k for k in dict.fromkeys(list(data.get("basis") or []) + FACT_TOKEN.findall(logic)) if k in keys][:10]
        if not basis:
            raise LLMError("basis 至少要引用一筆市場事實")
        counter = data.get("counter_evidence")
        counter = _strip_tokens(counter.strip(), set(basis))[:500] if isinstance(counter, str) and counter.strip() else None
        conf = data.get("confidence")
        conf = round(float(conf), 2) if isinstance(conf, (int, float)) and 0 <= conf <= 1 else None
        return MarketView(basis, _strip_tokens(logic, set(basis))[:1000], regime, stance, intent, (c_lo, c_hi), target,
                          counter, conf)


# ---------------------------------------------------------------- 研究

class LLMResearchAgent(LLMAgent):
    """量化工具先替 150 檔算好分數（規則版研究），LLM 依產業分批並行，針對前段、持股與有重大訊息的股票重新判斷。"""
    name = "research"
    BATCH = 14  # 每批檔數；同產業盡量放同一批

    def _prepare(self, ctx: RunContext, inputs: dict[str, Any]) -> list[list[str]]:
        from ..theme import universe_industries

        weak = list(inputs.get("weak") or [])
        prev = inputs.get("previous")
        self._retry = bool(weak and prev)
        base = prev if self._retry else RuleResearchAgent().run(ctx, inputs, None)
        self._base = {t.tickers[0]: t for t in base if t.tickers}
        self._weak = set(weak)
        ranked = sorted(self._base, key=lambda c: -(self._base[c].score or -9))
        if self._retry:
            focus = [c for c in weak if c in self._base]
        else:
            news = [c for c in ranked[:80] if any(f.topic == "intel" for f in ctx.facts.of(c))]
            focus = list(dict.fromkeys(ranked[:FOCUS] + sorted(ctx.held & set(self._base)) + news))
        self._focus = focus
        self._allowed = {c: set(self._base[c].premises) | {f.key for f in ctx.facts.of(c)} for c in focus}
        industry = universe_industries()
        ordered = sorted(focus, key=lambda c: (industry.get(c, ""), c))
        n = max(1, -(-len(ordered) // self.BATCH))
        size = -(-len(ordered) // n)
        return [ordered[i:i + size] for i in range(0, len(ordered), size)]

    def _system(self, ctx: RunContext) -> str:
        return (
            "你是台股主動式 ETF 的個股研究員。"
            f"ETF 主題：{ctx.settings.theme.name}。{ctx.settings.theme.description}\n"
            f"每檔股票附上量化工具算好的優質動能分數（{ctx.settings.strategy.describe()}，z 分數）、"
            "相關事實（價量含近期高低點、財報、月營收含較上月增減、重大訊息），以及工具偵測的矛盾訊號。"
            "請逐檔判斷 view（positive／neutral／negative）與 score（-3～3，可參考量化分數但要有自己的判斷，"
            "例如評價過高、短線過熱、獲利轉弱都可調整）；有重大訊息的股票必須評估該訊息的影響並引用它。"
            "logic 60–350 字，必須引用該股事實代號（寫成 {F12} 這種格式），說明判斷依據；"
            "conclusion 40 字內；counter_evidence 寫反方訊號（沒有就 null）。\n"
            + playbook() + "\n"
            + NO_INFER +
            '\n輸出 JSON：{"theses":[{"ticker":"2330","view":"positive","score":1.2,"premises":["F12"],'
            '"logic":"…","conclusion":"…","counter_evidence":"…"}]}，每檔都要輸出。'
        )

    def _user(self, ctx: RunContext, codes: list[str]) -> str:
        theme = ctx.theme.members if ctx.theme and ctx.theme.active else set()
        blocks = []
        for c in codes:
            t = self._base[c]
            facts = "\n".join(_fact_line(ctx, k) for k in sorted(self._allowed[c], key=lambda k: int(k[1:])))
            tag = "主題核心池" if c in theme else ("非核心池（衛星）" if theme else "")
            found = "；".join(x.text for x in conflicts(ctx.metrics[c])) if c in ctx.metrics else ""
            blocks.append(f"■ {ctx.name(c)} {tag} 量化分數 {t.score:+.2f}"
                          + (f"（反方審查意見：{t.counter_evidence}）" if c in self._weak and t.counter_evidence else "")
                          + f"\n工具偵測的矛盾：{found or '無'}\n{facts}")
        return f"交易日 {ctx.window.trade_date}，共 {len(codes)} 檔：\n\n" + "\n\n".join(blocks)

    def build_prompt(self, ctx: RunContext, inputs: dict[str, Any], feedback: list[str] | None) -> tuple[str, str]:
        self._prepare(ctx, inputs)
        return self._system(ctx), self._user(ctx, self._focus)

    def run(self, ctx: RunContext, inputs: dict[str, Any], feedback: list[str] | None = None) -> list[Thesis]:
        from concurrent.futures import ThreadPoolExecutor

        from ..llm.budget import reserve

        batches = self._prepare(ctx, inputs)
        system = self._system(ctx)
        for _ in batches:
            reserve(ctx, self.name, self.spec)  # 先在主執行緒佔好額度，額度不足就整批不呼叫
        items: list[Any] = []
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=len(batches)) as pool:
            futures = [pool.submit(self.call, ctx, system, self._user(ctx, b), feedback, f"b{i + 1}")
                       for i, b in enumerate(batches)]
            for i, fut in enumerate(futures):
                try:
                    data = fut.result().data
                    got = data.get("theses") if isinstance(data, dict) else None
                    items += got if isinstance(got, list) else []
                except Exception as exc:  # noqa: BLE001 - 單批失敗時其他批照用，最後看合格檔數
                    errors.append(f"第 {i + 1} 批（{len(batches[i])} 檔）失敗：{exc}")
        if errors:
            ctx.log("  ⚠ 研究 " + "；".join(e[:150] for e in errors))
        return self.parse(ctx, inputs, {"theses": items})

    def parse(self, ctx: RunContext, inputs: dict[str, Any], data: Any) -> list[Thesis]:
        items = data.get("theses") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise LLMError("輸出缺少 theses 陣列")
        out = dict(self._base)
        done: set[str] = set()
        for it in items:
            if not isinstance(it, dict) or it.get("ticker") not in self._focus or it["ticker"] in done:
                continue
            c = str(it["ticker"])
            allowed = self._allowed[c]
            logic = it.get("logic")
            if it.get("view") not in VIEWS or not isinstance(logic, str) or len(logic.strip()) < 30:
                continue
            premises = [k for k in dict.fromkeys(list(it.get("premises") or []) + FACT_TOKEN.findall(logic)) if k in allowed][:10]
            if not premises:
                continue
            score = it.get("score")
            score = max(-3.0, min(3.0, float(score))) if isinstance(score, (int, float)) and not isinstance(score, bool) else 0.0
            counter = it.get("counter_evidence")
            counter = _strip_tokens(counter.strip(), set(premises))[:500] if isinstance(counter, str) and counter.strip() else None
            conclusion = it.get("conclusion") if isinstance(it.get("conclusion"), str) else ""
            out[c] = Thesis(f"T:{c}", [c], premises, _strip_tokens(logic.strip(), set(premises))[:1000],
                            f"{ctx.name(c)}：{_strip_tokens(conclusion, set(premises))}"[:300], counter, it["view"],
                            round(score, 3), round(min(0.9, 0.5 + abs(score) / 6), 2), "research/llm")
            done.add(c)
        if len(done) < max(1, len(self._focus) * 0.6):
            raise LLMError(f"只有 {len(done)}/{len(self._focus)} 檔輸出合格，請每檔都輸出完整欄位並引用該股事實代號")
        if not self._retry:
            # 不在研究範圍的股票保留量化分數，但看法降為中性：今天的買進只來自 LLM 研究過的股票
            for c, t in out.items():
                if c not in done and t.view == "positive":
                    t.view = "neutral"
        else:
            # 退回重寫後仍沒解釋矛盾訊號的正向推論，看法降為中性
            for c in done:
                t = out[c]
                notes = conflict_notes(ctx, t)
                if notes:
                    t.view = "neutral"
                    t.counter_evidence = "；".join(filter(None, [t.counter_evidence, *notes, "重寫後仍未解釋，看法降為中性"]))[:500]
        return list(out.values())


# ---------------------------------------------------------------- 反方審查

class LLMReviewerAgent(LLMAgent):
    name = "reviewer"
    MAX_WEAK = 6  # LLM 自己挑的上限；程式偵測到的未解釋矛盾另外一律退回

    def build_prompt(self, ctx: RunContext, inputs: dict[str, Any], feedback: list[str] | None) -> tuple[str, str]:
        theses = [t for t in inputs["theses"] if t.author == "research/llm" and t.view == "positive"]
        self._keys = {t.key for t in inputs["theses"]}
        self._auto = {t.key: notes for t in inputs["theses"] if (notes := conflict_notes(ctx, t))}
        system = (
            "你是投資團隊的反方審查員（devil's advocate）。逐一檢查研究員的正向推論：\n"
            "1. 論據是否真的由引用的事實支持，數字有沒有被誤讀。\n"
            "2. 股價走勢與基本面是否矛盾而推論沒有解釋：例如營收年增很高但較上月大減、"
            "前波大漲後自高點大幅回落（可能是獲利了結）、評價過高、獲利衰退、流動性不足。"
            "「工具偵測的矛盾」由程式計算，推論沒有回應的會自動退回，你不必重複；請找出工具沒抓到的問題。\n"
            "3. 推論是否只是把數字複述一遍，沒有說明這些數字為什麼支持正向看法。\n"
            f"只挑出論據最薄弱的最多 {self.MAX_WEAK} 筆標為 weak，note（80 字內）具體寫出缺了什麼解釋；其餘不用輸出。\n"
            "研究員應遵守的" + playbook()
            + '\n輸出 JSON：{"reviews":[{"key":"T:2330","verdict":"weak","note":"…"}]}'
        )
        blocks = []
        for t in theses:
            found = [c.text for c in conflicts(ctx.metrics[t.tickers[0]])] if t.tickers and t.tickers[0] in ctx.metrics else []
            blocks.append(f"■ {t.key} 結論：{t.conclusion}\n推論：{t.logic}\n反方訊號：{t.counter_evidence or '無'}\n"
                          f"工具偵測的矛盾：{'；'.join(found) or '無'}\n事實：\n"
                          + "\n".join(_fact_line(ctx, k) for k in t.premises))
        return system, f"共 {len(blocks)} 筆正向推論：\n\n" + "\n\n".join(blocks or ["（無）"])

    def parse(self, ctx: RunContext, inputs: dict[str, Any], data: Any) -> list[Review]:
        items = data.get("reviews") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise LLMError("輸出缺少 reviews 陣列")
        weak = {}
        for it in items:
            if isinstance(it, dict) and it.get("key") in self._keys and it.get("verdict") == "weak":
                note = it.get("note") if isinstance(it.get("note"), str) else ""
                weak[it["key"]] = note.strip()[:120] or "論據不足"
        weak = dict(list(weak.items())[:self.MAX_WEAK])
        for k, notes in self._auto.items():
            weak[k] = "、".join(([weak[k]] if k in weak else []) + notes)[:120]
        return [Review(k, "weak", weak[k]) if k in weak else Review(k, "ok") for k in sorted(self._keys)]


# ---------------------------------------------------------------- 投資組合經理

class LLMPortfolioManagerAgent(LLMAgent):
    """LLM 決定買哪些、賣哪些、加減碼哪些並寫組合理由；權重與股數由工具依現金目標計算。"""
    name = "portfolio_manager"

    def build_prompt(self, ctx: RunContext, inputs: dict[str, Any], feedback: list[str] | None) -> tuple[str, str]:
        mv = inputs["market_view"]
        risk = ctx.settings.risk
        book, nav, prices = ctx.book, ctx.book.nav, ctx.prices
        theses = {t.tickers[0]: t for t in inputs["theses"] if len(t.tickers) == 1}
        self._theses = theses
        theme = ctx.theme.members if ctx.theme and ctx.theme.active else set()
        n_target = round((risk.min_holdings + risk.max_holdings) / 2)
        equity = 1 - mv.cash_target
        swaps = ctx.settings.strategy.max_swaps
        self._anchors = anchor_codes(ctx)
        self._cands = [c for c, t in theses.items() if c not in ctx.held and c in prices and c not in self._anchors
                       and (t.view == "positive" or (t.view == "neutral" and t.author.endswith("/llm")))
                       and prices[c] * 1000 <= 0.5 * equity / n_target * nav]
        self._cands.sort(key=lambda c: (theses[c].view != "positive", -(theses[c].score or 0)))
        min_theme = ctx.settings.theme.min_weight if theme else 0.0
        max_out = int(n_target * (1 - min(1.0, (min_theme + 0.05) / max(equity, 0.01)))) if theme else n_target
        self._limits = (n_target, max_out, theme)
        system = (
            "你是台股主動式 ETF 的投資組合經理，依市場策略與研究員的推論決定今天的持股名單。"
            f"ETF 主題：{ctx.settings.theme.name}。{ctx.settings.theme.description}\n"
            f"限制：交易後持股 {risk.min_holdings}–{risk.max_holdings} 檔（建議 {n_target} 檔左右）；"
            + (f"非主題核心池的股票最多 {max_out} 檔；" if theme else "")
            + (f"市值前 {len(self._anchors)} 大（{'、'.join(self._anchors)}）由工具依市值比例自動納入，不要列入買賣名單，也計入檔數；"
               if self._anchors else "")
            + f"已持有的股票除非推論轉為負向或排名明顯落後，否則續抱（每天最多賣出 {swaps} 檔；交易成本一買一賣約 0.585%，頻繁換股會侵蝕績效）；"
            "新買進只能從候選清單挑選（照抄清單中的代號）；優先選看法正向的，正向不足檔數下限時再用中性的補足。"
            "權重與股數會由工具依現金目標計算，你只需決定名單與加碼／減碼名單。"
            "logic（100–500 字）說明組合理由，可以引用市場事實代號（寫成 {F1} 格式）。"
            '\n輸出 JSON：{"sell":["代號"],"buy":["代號"],"overweight":["代號"],"underweight":["代號"],"logic":"…"}'
        )
        held_lines = [f"{ctx.name(c)} 權重 {book.shares(c) * prices.get(c, 0) / nav:.2%} 看法 {theses[c].view if c in theses else '無'}"
                      f" 分數 {(theses[c].score or 0) if c in theses else 0:+.2f}" + (" 核心池" if c in theme else "")
                      for c in sorted(ctx.held)]
        self._cands = self._cands[:45]
        cand_lines = [f"{ctx.name(c)} {theses[c].view} 分數 {theses[c].score:+.2f}" + (" 核心池" if c in theme else " 衛星")
                      + f"：{theses[c].conclusion}" for c in self._cands]
        user = (f"交易日 {ctx.window.trade_date}。市場策略：{mv.regime}／{mv.stance}／{mv.intent}，"
                f"目標現金比 {mv.cash_target:.0%}（區間 {mv.cash_range[0]:.0%}–{mv.cash_range[1]:.0%}）。"
                f"\n市場事實：\n" + "\n".join(_fact_line(ctx, k) for k in mv.basis)
                + f"\n\n目前持股 {len(held_lines)} 檔：\n" + ("\n".join(held_lines) or "（無，首日全現金）")
                + f"\n\n候選（未持有，代號 名稱 看法 分數）{len(cand_lines)} 檔：\n" + "\n".join(cand_lines))
        return system, user

    def parse(self, ctx: RunContext, inputs: dict[str, Any], data: Any) -> Any:
        if not isinstance(data, dict):
            raise LLMError("輸出必須是 JSON 物件")
        risk = ctx.settings.risk
        n_target, max_out, theme = self._limits

        def codes(name: str) -> list[str]:
            v = data.get(name) or []
            return [str(c) for c in v] if isinstance(v, list) else []

        pinned = set(risk.floors) | set(self._anchors)
        sell = [c for c in codes("sell") if c in ctx.held and c not in pinned]
        buy = [c for c in dict.fromkeys(codes("buy")) if c in self._cands]
        bad = [c for c in codes("buy") if c not in self._cands and c not in ctx.held and c not in pinned]
        if bad:
            raise LLMError(f"買進名單 {bad} 不在候選清單內")
        swaps = ctx.settings.strategy.max_swaps
        if len(sell) > swaps and not ctx.datapack.is_first_day:
            raise LLMError(f"一天最多賣出 {swaps} 檔，你賣出了 {len(sell)} 檔")
        after = (ctx.held - set(sell)) | set(buy) | pinned
        if not risk.min_holdings <= len(after) <= risk.max_holdings:
            raise LLMError(f"交易後持股 {len(after)} 檔，必須介於 {risk.min_holdings}–{risk.max_holdings}")
        if theme and sum(c not in theme for c in after) > max_out:
            raise LLMError(f"非核心池股票 {sum(c not in theme for c in after)} 檔，超過上限 {max_out}")
        logic = _str(data, "logic", 30, 800)
        mv = inputs["market_view"]
        choice = {"sell": sell, "buy": buy, "overweight": set(codes("overweight")), "underweight": set(codes("underweight")),
                  "logic": _strip_tokens(logic, set(mv.basis))}
        plan = RulePortfolioManagerAgent().run(ctx, inputs | {"choice": choice}, None)
        for t in plan.theses:
            t.author = t.author.replace("/rule", "/llm")
        plan.notes["choice"] = {k: sorted(v) if isinstance(v, (set, list)) else v for k, v in choice.items()}
        return plan
