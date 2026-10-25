"""策略參數回測搜尋：每組參數用規則版 Agent 跑一次多日演練，比較扣除交易成本後的績效。

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
                    combos.append((f"{name}|品質{float(q):.0%}|前{a}大|換{s}", strategy))
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
            **summarize(rows, split), "navs": [[r["trade_date"], r.get("nav")] for r in rows],
            "failures": [[r["trade_date"], r.get("error") or r.get("error_codes")] for r in rows if not r.get("ok")]}


def optimize(settings: Settings, start: dt.date, end: dt.date, split: dt.date | None, grid: Path,
             workers: int = 4, offline: bool = False) -> tuple[list[dict[str, Any]], Path]:
    combos = load_grid(grid, settings)
    out_dir = settings.paths.root / "rehearsals" / f"optimize_{start:%Y%m%d}_{end:%Y%m%d}_{grid.stem}"
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "work"
    print(f"▶ {len(combos)} 組參數、{start} ～ {end}" + (f"，前段到 {split}" if split else "") + "；LLM 呼叫 0 次")
    # 第一組在主程序連網跑，把行情與財報抓進快取；其餘離線平行
    results = [run_combo(settings, *combos[0], start, end, split, work / "c000", offline)]
    print(f"  [1/{len(combos)}] {combos[0][0]}  {_brief(results[0])}")
    with ProcessPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(run_combo, settings, name, strat, start, end, split, work / f"c{i:03d}", True): name
                   for i, (name, strat) in enumerate(combos[1:], 1)}
        for n, fut in enumerate(as_completed(futures), 2):
            results.append(fut.result())
            print(f"  [{n}/{len(combos)}] {results[-1]['name']}  {_brief(results[-1])}")
    lookup = dict(combos)
    for i, r in enumerate(results):
        if "error" in r or r.get("failed"):  # 平行時偶發個別交易日失敗（單獨跑不會）：在主程序重跑一次
            results[i] = run_combo(settings, r["name"], lookup[r["name"]], start, end, split, work / "retry", True)
            print(f"  重跑 {r['name']}  {_brief(results[i])}")
    shutil.rmtree(work, ignore_errors=True)
    key = "train_excess_pct" if split else "excess_pct"
    results.sort(key=lambda r: -(r.get(key) if r.get(key) is not None else -1e9))
    (out_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "results.md").write_text(format_table(results, split), encoding="utf-8")
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
    lines = [head, sep]
    for r in results:
        if "error" in r:
            lines.append(f"| {r['name']} | 失敗：{r['error'][:60]} |")
            continue
        line = (f"| {r['name']} | {r['ret_pct']:+.2f} | {r['excess_pct']:+.2f} | {r['mdd_pct']:.2f} | "
                f"{r['cost_pct']:.2f} | {r['turnover_avg_pct']:.2f} |")
        if split:
            line += f" {r.get('train_excess_pct', 0):+.2f} | {r.get('test_excess_pct', 0):+.2f} |"
        lines.append(line + (f"（{r['failed']} 天失敗）" if r.get("failed") else ""))
    return "\n".join(lines) + "\n"


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
            + "。名稱格式：動能天數組合|品質分數比重|市值前 N 大依市值配置|每日最多出清檔數。\n\n" + format_table(results, split))
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
