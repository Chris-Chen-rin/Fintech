"""策略參數回測搜尋：每組參數用規則版 Agent 跑一次多日演練，比較扣除交易成本後的績效。

※ 開發與研究用工具，不用於產生 D-Plan：每日交件流程（python -m agent run → run_daily）不會載入本模組，
  輸出只寫到 rehearsals/，不會進 out/。

- 搜尋本身不呼叫 LLM（規則版、離線快取），可以平行跑。
- 期間切成前段（挑參數）與後段（驗證）：只在前段好、後段崩的組合就是過度配適。
- --review 時把結果表交給投資組合經理用的模型（1 次呼叫）挑選並寫理由。
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import shutil
import statistics
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from .rehearse import INITIAL_NAV, rehearse
from .settings import PROJECT_ROOT, LLMSettings, Settings, SettingsError, StrategySettings, parse_strategy

GRID_FILE = PROJECT_ROOT / "config" / "optimize_grid.yaml"
SEP = "／"  # 不能用 |：名稱會放進 Markdown 表格
WINDOW = 24  # 初賽交易日數，用來算「任選連續 24 個交易日」的表現


def load_grid(path: Path, settings: Settings) -> list[tuple[str, StrategySettings]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    momentum = raw.get("momentum") or {}
    if not isinstance(momentum, dict) or not momentum:
        raise SettingsError(f"{path.name}：momentum 必須是 {{名稱: {{天數: 權重}}}}")
    combos: list[tuple[str, StrategySettings]] = []
    errors: list[str] = []
    for name, mom in momentum.items():
        for q in raw.get("quality_share") or [settings.strategy.quality_share]:
            for a in raw.get("anchor_top_n") or [0]:
                for s in raw.get("max_swaps") or [settings.strategy.max_swaps]:
                    strategy = parse_strategy({"momentum": mom, "quality_share": q, "anchor_top_n": a, "max_swaps": s},
                                              settings.risk, errors)
                    combos.append((f"{name}{SEP}品質{float(q):.0%}{SEP}前{a}大{SEP}換{s}", strategy))
    exclude = set(raw.get("exclude") or [])
    unknown = exclude - {n for n, _ in combos}
    if unknown:
        errors.append("exclude 裡的名稱不在組合中：" + "、".join(sorted(unknown)))
    combos = [c for c in combos if c[0] not in exclude]
    if errors:
        raise SettingsError(f"{path.name} 有誤：\n" + "\n".join(f"  - {e}" for e in sorted(set(errors))))
    return combos


def summarize(rows: list[dict[str, Any]], split: dt.date | None) -> dict[str, Any]:
    ok = [r for r in rows if "nav" in r]
    navs = [INITIAL_NAV] + [r["nav"] for r in ok]
    peak, mdd = navs[0], 0.0
    for v in navs:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    end = ok[-1] if ok else {}
    out: dict[str, Any] = {
        "days": len(rows), "failed": sum(1 for r in rows if not r.get("ok")),
        "ret_pct": end.get("ret_pct"), "taiex_pct": end.get("taiex_ret_pct"),
        "excess_pct": round(end["ret_pct"] - end["taiex_ret_pct"], 2) if end else None,
        "mdd_pct": round(mdd * 100, 2),
        "cost_pct": round(sum(r.get("cost", 0) for r in ok) / INITIAL_NAV * 100, 2),
        "turnover_avg_pct": round(statistics.fmean(r.get("turnover", 0) for r in ok[1:]) * 100, 2) if len(ok) > 1 else 0.0,
        "trade_days": sum(1 for r in ok[1:] if r.get("orders")),
    }
    if split and ok:
        first = [r for r in ok if dt.date.fromisoformat(r["trade_date"]) <= split]
        if first and len(first) < len(ok):
            mid = first[-1]
            nav_mid, tx_mid = mid["nav"], 1 + mid["taiex_ret_pct"] / 100
            out["train_ret_pct"] = mid["ret_pct"]
            out["train_excess_pct"] = round(mid["ret_pct"] - mid["taiex_ret_pct"], 2)
            test_ret = (end["nav"] / nav_mid - 1) * 100
            test_tx = ((1 + end["taiex_ret_pct"] / 100) / tx_mid - 1) * 100
            out["test_ret_pct"] = round(test_ret, 2)
            out["test_excess_pct"] = round(test_ret - test_tx, 2)
    tx = [1 + r["taiex_ret_pct"] / 100 for r in ok]
    excess = [((ok[i + WINDOW]["nav"] / ok[i]["nav"]) - tx[i + WINDOW] / tx[i]) * 100 for i in range(len(ok) - WINDOW)]
    if excess:
        out["w24_win_pct"] = round(sum(e > 0 for e in excess) / len(excess) * 100, 1)
        out["w24_median_pct"] = round(statistics.median(excess), 2)
        out["w24_worst_pct"] = round(min(excess), 2)
    return out


def run_combo(settings: Settings, name: str, strategy: StrategySettings, start: dt.date, end: dt.date,
              split: dt.date | None, folder: Path, offline: bool) -> dict[str, Any]:
    s = replace(settings, llm=LLMSettings(), strategy=strategy)
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            rows = rehearse(s, start, end, offline=offline, folder=folder, announcements=False)
        except Exception as exc:  # noqa: BLE001 - 一組失敗不影響其他組
            where = traceback.extract_tb(exc.__traceback__)[-1]
            return {"name": name, "error": f"{exc}（{Path(where.filename).name}:{where.lineno}）"}
    shutil.rmtree(folder, ignore_errors=True)
    return {"name": name, "strategy": {"momentum": strategy.momentum, "quality_share": strategy.quality_share,
                                       "anchor_top_n": strategy.anchor_top_n, "max_swaps": strategy.max_swaps},
            **summarize(rows, split), "navs": [[r["trade_date"], r.get("nav"), r.get("taiex_ret_pct")] for r in rows],
            "failures": [[r["trade_date"], r.get("error") or r.get("error_codes")] for r in rows if not r.get("ok")]}


def optimize(settings: Settings, start: dt.date, end: dt.date, split: dt.date | None, grid: Path,
             workers: int = 4, offline: bool = False) -> tuple[list[dict[str, Any]], Path]:
    combos = load_grid(grid, settings)
    out_dir = settings.paths.root / "rehearsals" / f"optimize_{start:%Y%m%d}_{end:%Y%m%d}_{grid.stem}"
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "work"
    print(f"▶ {len(combos)} 組參數、{start} ～ {end}" + (f"，前段到 {split}" if split else "") + "；LLM 呼叫 0 次")
    t0 = time.monotonic()
    # 第一組在主程序連網跑，把行情與財報抓進快取；其餘離線平行（參數表第一組要放回看天數最長的）
    results = [run_combo(settings, *combos[0], start, end, split, work / "c000", offline)]
    first = time.monotonic() - t0
    print(f"  [1/{len(combos)}] {combos[0][0]}  {_brief(results[0])}  （{first / 60:.1f} 分）")
    t1 = time.monotonic()
    with ProcessPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(run_combo, settings, name, strat, start, end, split, work / f"c{i:03d}", True): name
                   for i, (name, strat) in enumerate(combos[1:], 1)}
        for n, fut in enumerate(as_completed(futures), 2):
            results.append(fut.result())
            done = n - 1
            eta = (time.monotonic() - t1) / done * (len(combos) - n)
            print(f"  [{n}/{len(combos)}] {results[-1]['name']}  {_brief(results[-1])}  "
                  f"（已 {(time.monotonic() - t0) / 60:.0f} 分，預估剩 {eta / 60:.0f} 分）")
    shutil.rmtree(work, ignore_errors=True)
    bad = [r for r in results if "error" in r or r.get("failed")]
    if bad:
        print(f"  ⚠ {len(bad)} 組中斷或有交易日沒通過檢查（當天不下單，績效失真），原因見 results.json 的 failures")
    print(f"  總耗時 {(time.monotonic() - t0) / 60:.0f} 分")
    key = "train_excess_pct" if split else "excess_pct"
    results.sort(key=lambda r: -(r.get(key) if r.get(key) is not None else -1e9))
    (out_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "results.md").write_text(format_report(results, split), encoding="utf-8")
    return results, out_dir


def _brief(r: dict[str, Any]) -> str:
    if "error" in r:
        return f"✗ {r['error']}"
    return f"報酬 {r['ret_pct']:+.2f}%（超額 {r['excess_pct']:+.2f}）回檔 {r['mdd_pct']:.2f}% 成本 {r['cost_pct']:.2f}%"


def format_table(results: list[dict[str, Any]], split: dt.date | None) -> str:
    head = "| 參數組合 | 報酬% | 超額% | 最大回檔% | 成本% | 日均周轉% |"
    sep = "|---|---|---|---|---|---|"
    if split:
        head += " 前段超額% | 後段超額% |"
        sep += "---|---|"
    windows = any("w24_win_pct" in r for r in results)
    if windows:
        head += " 24日勝率% | 24日中位超額% |"
        sep += "---|---|"
    lines = [head, sep]
    for r in results:
        if "error" in r:
            lines.append(f"| {r['name']} | 失敗：{r['error'][:60]} |")
            continue
        line = (f"| {r['name']} | {r['ret_pct']:+.2f} | {r['excess_pct']:+.2f} | {r['mdd_pct']:.2f} | "
                f"{r['cost_pct']:.2f} | {r['turnover_avg_pct']:.2f} |")
        if split:
            line += f" {r.get('train_excess_pct', 0):+.2f} | {r.get('test_excess_pct', 0):+.2f} |"
        if windows:
            line += (f" {r['w24_win_pct']:.0f} | {r['w24_median_pct']:+.2f} |" if "w24_win_pct" in r else " — | — |")
        lines.append(line + (f"（{r['failed']} 天失敗）" if r.get("failed") else ""))
    return "\n".join(lines) + "\n"


COLUMNS = """\
| 欄位 | 意思 | 怎麼看 |
|---|---|---|
| 參數組合 | 這一列用的策略參數，格式見下方「名稱怎麼讀」 | |
| 報酬% | 整段期間的淨值報酬：期末淨值 ÷ 初始 10 億 − 1。已扣手續費與證交稅；每天用前一日資料決策、以當天收盤價成交 | 越高越好 |
| 超額% | 報酬% − 同期加權指數漲幅 | 正數才是贏大盤；比賽和其他隊伍比，大盤是最基本的門檻 |
| 最大回檔% | 淨值從任一高點跌到之後最低點的最大跌幅 | 越接近 0 越好；代表最糟時曾經虧掉多少 |
| 成本% | 整段期間累計的手續費（買賣各 0.1425%）與證交稅（賣出 0.3%）÷ 初始 10 億 | 越低越好；已經從報酬% 扣掉，這欄是讓你看換股吃掉多少 |
| 日均周轉% | 第 2 天起，每天買賣成交金額 ÷ 當天淨值 的平均（首日建倉不算） | 越高代表換股越頻繁；成本% 大致跟它成正比 |
| 前段超額% | 從起日到切分日的報酬 − 同期大盤（用來挑參數的期間） | 表格依這欄由高到低排序 |
| 後段超額% | 切分日之後的報酬 − 同期大盤（以切分日淨值為起點；挑參數時沒看過的期間） | 前段好、後段也好才可信；前段好、後段差就是過度配適 |
| 24日勝率% | 把回測期間切成所有「連續 24 個交易日」（初賽長度）的區間，其中贏過大盤的比例 | 越高越好；最接近「比賽期間贏大盤的機率」 |
| 24日中位超額% | 上述所有 24 日區間超額報酬的中位數 | 越高越好；代表一般情況下一場比賽能領先大盤多少 |
"""


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    def ranks(v: list[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        out = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                out[order[k]] = (i + j) / 2
            i = j + 1
        return out
    if len(xs) < 3:
        return None
    rx, ry = ranks(xs), ranks(ys)
    try:
        return statistics.correlation(rx, ry)
    except statistics.StatisticsError:
        return None


def _names_legend(ok: list[dict[str, Any]]) -> str:
    sets: dict[str, dict] = {}
    for r in ok:
        sets.setdefault(r["name"].split(SEP)[0], r["strategy"]["momentum"])
    rows = ["| 動能組合 | 天數：權重 |", "|---|---|"]
    rows += [f"| {n} | " + "、".join(f"{d} 日 {float(w):.0%}" for d, w in sorted(m.items(), key=lambda x: int(x[0]))) + " |"
             for n, m in sets.items()]
    return (
        f"名稱格式：`動能組合{SEP}品質比重{SEP}前N大{SEP}換S`\n\n"
        "- **動能組合**：用過去幾個交易日的報酬算動能分數，各天數的權重如下表。\n"
        "- **品質比重**：選股分數中品質（ROE、EPS 年增、累計營收年增、現金流量）占多少，其餘是動能。"
        "品質 0% = 只看動能。\n"
        "- **前N大**：市值前 N 大固定持有、依市值比例配置（受個股上限限制），超過上限的部分改配給分數高的股票。"
        "前 0 大 = 不錨定，完全依分數。\n"
        "- **換S**：每天最多出清幾檔，用來限制換股成本。\n\n" + "\n".join(rows) + "\n"
    )


def _group(ok: list[dict[str, Any]], label: str, key) -> list[str]:
    groups: dict[Any, list[dict[str, Any]]] = {}
    for r in ok:
        groups.setdefault(key(r), []).append(r)
    lines = [f"| {label} | 組數 | 平均超額% | 平均最大回檔% | 平均成本% |", "|---|---|---|---|---|"]
    for k in sorted(groups):
        g = groups[k]
        lines.append(f"| {k} | {len(g)} | {statistics.fmean(r['excess_pct'] for r in g):+.2f} | "
                     f"{statistics.fmean(r['mdd_pct'] for r in g):.2f} | {statistics.fmean(r['cost_pct'] for r in g):.2f} |")
    return lines


def robust_ranking(ok: list[dict[str, Any]]) -> list[tuple[float, float, dict[str, Any]]]:
    rows = [r for r in ok if "w24_median_pct" in r]
    parts = {r["name"]: r["name"].split(SEP) for r in rows}
    out = []
    for r in rows:
        near = [o["w24_median_pct"] for o in rows if o is not r
                and sum(a != b for a, b in zip(parts[r["name"]], parts[o["name"]])) == 1]
        if near:
            avg = statistics.fmean(near)
            out.append((round((r["w24_median_pct"] + avg) / 2, 2), round(avg, 2), r))
    return sorted(out, key=lambda x: -x[0])


def summary(results: list[dict[str, Any]], split: dt.date | None) -> str:
    ok = [r for r in results if "error" not in r and r.get("ret_pct") is not None]
    if not ok:
        return "沒有成功的組合。\n"
    taiex = ok[0]["taiex_pct"]
    beat = [r for r in ok if r["excess_pct"] > 0]
    lines = []
    flawed = [r for r in ok if r.get("failed")]
    if flawed or len(ok) < len(results):
        lines.append(f"- ⚠ **{len(results) - len(ok)} 組中斷、{len(flawed)} 組有交易日沒通過檢查**"
                     "（沒通過的那天不下單），這些組合的數字失真，要先修程式再重跑。")
    lines.append(f"- 共 {len(ok)} 組成功（{len(results) - len(ok)} 組失敗）；同期加權指數 {taiex:+.2f}%，"
                 f"贏過大盤的有 **{len(beat)} 組**。")
    best = max(ok, key=lambda r: r["ret_pct"])
    safe = max(ok, key=lambda r: r["mdd_pct"])
    cheap = min(ok, key=lambda r: r["cost_pct"])
    lines.append(f"- 總報酬最高：{best['name']}（{best['ret_pct']:+.2f}%，回檔 {best['mdd_pct']:.2f}%）")
    lines.append(f"- 回檔最小：{safe['name']}（{safe['mdd_pct']:.2f}%，報酬 {safe['ret_pct']:+.2f}%）")
    lines.append(f"- 成本最低：{cheap['name']}（{cheap['cost_pct']:.2f}%，報酬 {cheap['ret_pct']:+.2f}%）")
    two = [r for r in ok if r.get("train_excess_pct") is not None and r.get("test_excess_pct") is not None]
    if split and len(two) >= 3:
        rho = _spearman([r["train_excess_pct"] for r in two], [r["test_excess_pct"] for r in two])
        by_test = sorted(two, key=lambda r: -r["test_excess_pct"])
        top = max(two, key=lambda r: r["train_excess_pct"])
        lines.append(f"- 前段第一：{top['name']}（前段 {top['train_excess_pct']:+.2f}%），"
                     f"後段排第 {by_test.index(top) + 1}/{len(two)}（{top['test_excess_pct']:+.2f}%）")
        if rho is not None:
            verdict = ("前段排名對後段有參考價值" if rho >= 0.3 else
                       "前段排名幾乎無法預測後段，挑參數要保守" if rho > -0.3 else
                       "前段好的組合後段反而差，**過度配適風險高**，不要只挑前段第一")
            lines.append(f"- 前段與後段排名相關係數 {rho:+.2f}（+1 完全一致、0 無關、−1 完全相反）：{verdict}")
        third = max(1, len(two) // 3)
        good_train = {r["name"] for r in sorted(two, key=lambda r: -r["train_excess_pct"])[:third]}
        good_test = {r["name"] for r in by_test[:third]}
        steady = sorted((r for r in two if r["name"] in good_train & good_test), key=lambda r: -r["mdd_pct"])
        if steady:
            lines.append("- 前段與後段超額都在前 1/3 的組合（較穩定，依回檔由小到大）：")
            lines += [f"  - {r['name']}：報酬 {r['ret_pct']:+.2f}%、回檔 {r['mdd_pct']:.2f}%、成本 {r['cost_pct']:.2f}%、"
                      f"前段 {r['train_excess_pct']:+.2f}%／後段 {r['test_excess_pct']:+.2f}%" for r in steady]
        else:
            lines.append("- 沒有組合在前段與後段都排進前 1/3。")
    robust = robust_ranking(ok)
    if robust:
        lines += ["", "穩健候選（依穩健分數；穩健分數 = 自己與相鄰組合的 24 日中位超額各占一半。"
                  "相鄰組合 = 只差一個參數的組合，分數高代表附近的參數也好、不是碰巧）：", "",
                  "| 參數組合 | 穩健分數 | 24日中位超額% | 相鄰平均% | 24日勝率% | 最大回檔% | 成本% |",
                  "|---|---|---|---|---|---|---|"]
        lines += [f"| {r['name']} | {score:+.2f} | {r['w24_median_pct']:+.2f} | {near:+.2f} | {r['w24_win_pct']:.0f} | "
                  f"{r['mdd_pct']:.2f} | {r['cost_pct']:.2f} |" for score, near, r in robust[:5]]
    lines += ["", "各參數的平均表現（同一參數值底下所有組合的平均，看單一參數的整體影響）：", ""]
    lines += _group(ok, "市值錨定", lambda r: f"前 {r['strategy']['anchor_top_n']:>2} 大") + [""]
    lines += _group(ok, "品質比重", lambda r: f"{float(r['strategy']['quality_share']):.0%}") + [""]
    lines += _group(ok, "動能組合", lambda r: r["name"].split(SEP)[0]) + [""]
    if len({r["strategy"]["max_swaps"] for r in ok}) > 1:
        lines += _group(ok, "每日出清上限", lambda r: f"{r['strategy']['max_swaps']} 檔") + [""]
    lines += ["注意：回測只用規則版 Agent（不含 LLM 判斷），期間短、只有一段行情；"
              "過去表現不代表未來，選參數時優先考慮前後段都穩定、回檔與成本低的組合，差距 1 個百分點內視為沒有差別。"]
    return "\n".join(lines) + "\n"


def format_report(results: list[dict[str, Any]], split: dt.date | None) -> str:
    ok = [r for r in results if "error" not in r and r.get("navs")]
    period = f"{ok[0]['navs'][0][0]} ～ {ok[0]['navs'][-1][0]}" if ok else ""
    order = "前段超額" if split else "超額"
    head = (f"# 策略參數回測結果\n\n期間 {period}" + (f"，前段到 {split}（挑參數）、之後為後段（驗證）" if split else "")
            + f"；初始淨值 10 億；表格依「{order}%」由高到低排序。\n\n")
    return (head + "## 結果表\n\n" + format_table(results, split) + "\n## 欄位說明\n\n" + COLUMNS
            + "\n## 名稱怎麼讀\n\n" + _names_legend(ok) + "\n## 統整\n\n" + summary(results, split))


def review(settings: Settings, results: list[dict[str, Any]], split: dt.date | None, out_dir: Path) -> dict[str, Any]:
    """把結果表交給 LLM 挑選（1 次呼叫，計入每日額度）。"""
    from .llm.budget import reserve
    from .llm.providers import make_client

    chain = settings.llm.chain("portfolio_manager")
    if not chain:
        raise SettingsError("沒有設定 portfolio_manager 的 LLM，無法審閱")
    names = [r["name"] for r in results if "error" not in r]
    system = (
        "你是台股主動式 ETF 的投資長，要從回測結果中選出正式使用的策略參數。"
        "原則：比賽只有 24 個交易日、以最終淨值排名；交易成本一買一賣約 0.585%，頻繁換股會侵蝕績效；"
        "回測期間短，要避免過度配適——優先選前段與後段都穩定、回檔與成本較低的組合，而不是報酬最高的單一組合；"
        "差距在 1 個百分點內視為沒有差別，此時選較簡單、周轉較低的。"
        '輸出 JSON：{"choice":"參數組合名稱（照抄）","runner_up":"次佳名稱","reason":"200–500 字",'
        '"risks":"100–300 字，這組參數在什麼市況會表現差"}'
    )
    user = (f"回測期間 {results[0]['navs'][0][0]} ～ {results[0]['navs'][-1][0]}"
            + (f"，前段到 {split}（用來挑參數），之後為後段（驗證）" if split else "")
            + "。名稱格式：動能天數組合／品質分數比重／市值前 N 大依市值配置／每日最多出清檔數。\n\n" + format_table(results, split))
    ctx = SimpleNamespace(settings=settings, llm_calls=0)
    last_exc: Exception | None = None
    for spec in chain:
        try:
            reserve(ctx, "optimize_review", spec)
            res = make_client(spec).complete_json(system, user)
            data = res.data if isinstance(res.data, dict) else {}
            if data.get("choice") not in names:
                raise ValueError(f"choice {data.get('choice')!r} 不在結果表中")
            data["model"] = spec.label
            text = (f"# 策略參數審閱（{spec.label}）\n\n選擇：**{data['choice']}**（次佳：{data.get('runner_up')}）\n\n"
                    f"## 理由\n\n{data.get('reason')}\n\n## 風險\n\n{data.get('risks')}\n\n## 結果表\n\n" + format_table(results, split))
            (out_dir / "review.md").write_text(text, encoding="utf-8")
            return data
        except Exception as exc:  # noqa: BLE001 - 換下一個模型
            last_exc = exc
            print(f"  ⚠ {spec.label} 審閱失敗：{exc}")
    raise RuntimeError(f"所有模型審閱失敗：{last_exc}")
