from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from .settings import SettingsError, code_version, get_secret, load_settings
from .trading_calendar import TradingCalendar, now_tpe, to_tpe


def doctor(args: argparse.Namespace) -> int:
    try:
        settings = load_settings(args.settings)
    except SettingsError as exc:
        print(f"✗ {exc}")
        return 1

    moment = to_tpe(dt.datetime.fromisoformat(args.now)) if args.now else now_tpe()
    cal = TradingCalendar(settings.holidays)
    window = cal.target_window(moment)
    settings.paths.ensure()

    print(f"✓ 設定檔：{settings.source_file}")
    print(f"  隊伍代號：{settings.team_id}")
    print(f"  主題：{settings.theme.name or '（未設定）'}，標的 {len(settings.theme.tickers)} 檔")
    print(f"  LLM：{settings.llm.provider} {settings.llm.model}".rstrip())
    if settings.llm.api_key_env:
        found = get_secret(settings.llm.api_key_env) is not None
        print(f"  金鑰 {settings.llm.api_key_env}：{'已找到' if found else '找不到'}")
    print(f"  資料根目錄：{settings.paths.root}")
    print(f"  程式版本：{code_version()}")
    print(f"  現在（台北）：{moment:%Y-%m-%d %H:%M:%S}")
    print(f"  {window.describe(moment)}")
    days = cal.competition_days()
    print(f"  初賽交易日：{len(days)} 天（{days[0]} ～ {days[-1]}）" if days else "  初賽交易日：0 天")
    if not window.in_competition:
        print("  ⚠ 目標交易日不在初賽期間內")
    for w in settings.warnings:
        print(f"  ⚠ {w}")
    return 0


def run(args: argparse.Namespace) -> int:
    from .datapack import DataPackError
    from .framework.runner import AgentFailure
    from .run_daily import RunError, RunOptions, run_daily
    from .tools.http import FetchError

    try:
        settings = load_settings(args.settings)
    except SettingsError as exc:
        print(f"✗ {exc}")
        return 1
    for w in settings.warnings:
        print(f"⚠ {w}")
    opts = RunOptions(
        datapack_path=args.datapack,
        mock_first_day=args.mock_first_day,
        now=dt.datetime.fromisoformat(args.now) if args.now else None,
        offline=args.offline,
        write_out=not args.no_out,
    )
    try:
        result = run_daily(settings, opts)
    except (DataPackError, FetchError, RunError, AgentFailure) as exc:
        print(f"✗ {exc}")
        return 1

    report = result.report
    print()
    print(report.format_text())
    print()
    print(f"存檔資料夾：{result.run_dir}")
    if result.ok:
        if result.out_path:
            print(f"✓ D-Plan 已輸出：{result.out_path}")
            print("  下一步：到後台「文件繳交」上傳這個檔案，確認收件層檢核通過後按「送出」，再到「當日委託」核對。")
        else:
            print(f"✓ D-Plan 通過檢查（未輸出到 out/）：{result.dplan_path}")
        return 0
    print(f"✗ D-Plan 未通過檢查，沒有輸出到 out/。請看 {result.run_dir / 'check_report.txt'}，修程式或設定後重跑（不要手改 JSON）。")
    return 1


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m agent", description="AI Agent 基金經理人")
    parser.add_argument("--settings", type=Path, help="設定檔路徑（預設 config/settings.yaml 或環境變數 AGENT_SETTINGS）")
    sub = parser.add_subparsers(dest="command", required=True)
    p_doctor = sub.add_parser("doctor", help="檢查設定、路徑、程式版本與目前的繳交時段")
    p_doctor.add_argument("--now", help="以指定時間當作現在（台北時間），測試用")
    p_doctor.set_defaults(func=doctor)
    p_run = sub.add_parser("run", help="產生當日 D-Plan（資料包 → Agent → 風控 → 檢查器 → out/）")
    p_run.add_argument("--datapack", type=Path, help="資料包路徑；省略時到 inbox/ 找 trade_date 相符的檔案")
    p_run.add_argument("--mock-first-day", action="store_true", help="用規格書的首日資料包（全現金 10 億）演練")
    p_run.add_argument("--now", help="以指定時間當作現在（台北時間），演練用")
    p_run.add_argument("--offline", action="store_true", help="只用快取的行情，不連網")
    p_run.add_argument("--no-out", action="store_true", help="通過檢查也不複製到 out/（演練用）")
    p_run.set_defaults(func=run)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
