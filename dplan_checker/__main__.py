from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from . import rules as R
from .checker import CheckOptions, check_dplan


def _parse_now(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=R.TPE)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m dplan_checker",
        description="D-Plan 繳交前檢查：格式、引用鏈、決策覆蓋、委託推導、超賣、現金、持股檔數、權重、姿態一致性、主動投資比率等。",
    )
    p.add_argument("dplan", type=Path, help="D-Plan JSON 檔")
    p.add_argument("--datapack", type=Path, help="當日從後台下載的資料包 JSON（強烈建議提供，否則部位相關檢查全部略過）")
    p.add_argument("--prices", type=Path, help="前日收盤價（JSON 或 CSV）；新買進、資料包沒有的標的需要")
    p.add_argument("--active-etf", type=Path, help="主動型 ETF 前 15 大持股權重 JSON")
    p.add_argument("--theme", type=Path, help="ETF 主題設定 JSON：{name, tickers, min_weight}")
    p.add_argument("--team-id", help="主辦方配發的隊伍代號，用來比對 team_id")
    p.add_argument("--universe", type=Path, default=R.DEFAULT_UNIVERSE, help="150 檔標的清單（預設內建）")
    p.add_argument("--schema", type=Path, default=R.DEFAULT_SCHEMA, help="D-Plan JSON Schema（預設內建 v4.3）")
    p.add_argument("--now", type=_parse_now, help="以指定時間當作「現在」（台北時間），測試用")
    p.add_argument("--holiday", action="append", default=[], type=dt.date.fromisoformat, help="休市日 YYYY-MM-DD，可重複指定")
    p.add_argument("--json", action="store_true", help="以 JSON 輸出結果")
    p.add_argument("--strict", action="store_true", help="有警告也視為失敗（exit code 1）")
    return p


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    options = CheckOptions(
        datapack_path=args.datapack,
        prices_path=args.prices,
        universe_path=args.universe,
        active_etf_holdings_path=args.active_etf,
        theme_path=args.theme,
        schema_path=args.schema,
        team_id=args.team_id,
        now=args.now,
        holidays=R.KNOWN_HOLIDAYS | frozenset(args.holiday),
    )
    report = check_dplan(args.dplan, options)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.format_text())
    if not report.ok or (args.strict and report.warnings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
