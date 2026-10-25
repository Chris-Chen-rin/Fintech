from __future__ import annotations

import datetime as dt
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from jsonschema import Draft202012Validator

from . import rules as R
from .loaders import (
    InputError,
    load_active_etf_codes,
    load_active_etf_holdings,
    load_json,
    load_prices,
    load_theme,
    load_universe,
)
from .portfolio import Book, Order, Position, active_share, simulate, target_shares
from .report import DISQUALIFY, MISSED, REJECT, SEVERE, VIOLATION, Report

COVERAGE_CONSEQUENCE = "後台可能直接退件；若送出，當日委託整批作廢並記警告（dplan_decision_coverage）"
REF_CONSEQUENCE = REJECT + "；若漏網送出，當日委託整批作廢並記警告（dplan_reference_chain）"
ID_MENTION = re.compile(r"(?<![A-Za-z0-9_])([SOID])(\d{1,3})(?![0-9])")
TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?\+08:00$")


@dataclass
class CheckOptions:
    datapack_path: Path | None = None
    prices_path: Path | None = None
    universe_path: Path = R.DEFAULT_UNIVERSE
    active_etf_list_path: Path = R.DEFAULT_ACTIVE_ETFS
    active_etf_holdings_path: Path | None = None
    theme_path: Path | None = None
    schema_path: Path = R.DEFAULT_SCHEMA
    team_id: str | None = None
    now: dt.datetime | None = None
    holidays: frozenset[dt.date] = R.KNOWN_HOLIDAYS


def check_dplan(path: str | Path, options: CheckOptions | None = None) -> Report:
    return DPlanChecker(Path(path), options or CheckOptions()).run()


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def _money(x: float) -> str:
    return f"{x:,.0f}"


def _fmt_path(parts: Iterable[Any]) -> str:
    out = ""
    for p in parts:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else str(p))
    return out


class DPlanChecker:
    def __init__(self, path: Path, opts: CheckOptions):
        self.path = path
        self.opts = opts
        self.r = Report()
        self.doc: dict[str, Any] = {}
        self.now = opts.now or dt.datetime.now(R.TPE)
        self.universe: dict[str, str] = {}
        self.datapack: dict[str, Any] | None = None
        self.external_prices: dict[str, float] = {}
        self.etf_holdings: dict[str, dict[str, float]] | None = None
        self.theme: dict[str, Any] | None = None
        self.book: Book | None = None
        self.price_of: dict[str, float] = {}
        self.trade_date: dt.date | None = None
        self.completed_at: dt.datetime | None = None

    # ------------------------------------------------------------------ 流程

    def run(self) -> Report:
        if not self._load_dplan():
            return self.r
        self._load_inputs()
        self._check_filename()
        self._check_schema()
        self._check_top_level()
        self._check_times()
        self._check_ids()
        self._check_references()
        self._check_sources()
        self._check_observations()
        self._check_market_view()
        self._check_inferences()
        self._check_leftover_text()
        self._check_metadata()
        self._check_decisions_static()
        self._check_orders_static()
        self._check_universe()
        if self._prepare_portfolio():
            self._check_coverage()
            self._check_derivation()
            self._check_post_trade()
        return self.r

    # ------------------------------------------------------------------ 共用

    def _items(self, key: str) -> list[tuple[int, dict[str, Any]]]:
        value = self.doc.get(key)
        if not isinstance(value, list):
            return []
        return [(i, x) for i, x in enumerate(value) if isinstance(x, dict)]

    def _ids(self, key: str) -> set[str]:
        id_key = R.ID_PREFIX[key][0]
        return {x[id_key] for _, x in self._items(key) if isinstance(x.get(id_key), str)}

    @staticmethod
    def _str_list(value: Any) -> list[str]:
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

    def _ts(self, value: Any, path: str) -> dt.datetime | None:
        if not isinstance(value, str) or not TIMESTAMP_RE.match(value):
            return None
        try:
            return dt.datetime.fromisoformat(value)
        except ValueError:
            self.r.error("TIME_INVALID", f"時間 {value!r} 不是真實存在的日期時間", path, REJECT)
            return None

    def _decisions(self) -> list[tuple[int, dict[str, Any]]]:
        return self._items("decisions")

    def _decision_by_id(self) -> dict[str, dict[str, Any]]:
        return {d["decision_id"]: d for _, d in self._decisions() if isinstance(d.get("decision_id"), str)}

    def _name(self, ticker: str) -> str:
        name = self.universe.get(ticker)
        return f"{ticker} {name}" if name else ticker

    def _is_trading_day(self, day: dt.date) -> bool:
        return day.weekday() < 5 and day not in self.opts.holidays

    def _prev_trading_day(self, day: dt.date) -> dt.date:
        day -= dt.timedelta(days=1)
        while not self._is_trading_day(day):
            day -= dt.timedelta(days=1)
        return day

    # ------------------------------------------------------------------ 讀檔

    def _load_dplan(self) -> bool:
        if not self.path.exists():
            self.r.error("FILE_NOT_FOUND", f"找不到檔案 {self.path}")
            return False
        try:
            loaded = load_json(self.path)
        except InputError as exc:
            self.r.error("JSON_INVALID", str(exc), consequence=REJECT)
            return False
        if loaded.has_bom:
            self.r.warn("JSON_BOM", "檔案開頭有 UTF-8 BOM，部分解析器會判定為非法 JSON，建議以無 BOM 的 UTF-8 輸出")
        for key in sorted(set(loaded.duplicate_keys)):
            self.r.error("JSON_DUPLICATE_KEY", f"同一物件內出現重複的鍵 {key!r}，伺服器只會採用其中一個值",
                         consequence="伺服器解析結果可能與預期不同")
        if not isinstance(loaded.data, dict):
            self.r.error("JSON_NOT_OBJECT", "最外層必須是 JSON 物件", consequence=REJECT)
            return False
        self.doc = loaded.data
        self.r.summary["檔案大小"] = f"{loaded.size_bytes:,} bytes"
        return True

    def _load_inputs(self) -> None:
        try:
            self.universe = load_universe(self.opts.universe_path)
        except (InputError, OSError, KeyError) as exc:
            self.r.error("INPUT_UNIVERSE", f"無法讀取 150 檔標的清單：{exc}")

        if self.opts.datapack_path:
            try:
                data = load_json(self.opts.datapack_path).data
                if isinstance(data, dict):
                    self.datapack = data
                else:
                    self.r.error("INPUT_DATAPACK", "資料包最外層必須是 JSON 物件")
            except InputError as exc:
                self.r.error("INPUT_DATAPACK", f"無法讀取資料包：{exc}")
        else:
            self.r.skip("未提供資料包（--datapack）：略過決策覆蓋、委託推導、超賣、現金、持股檔數、權重、姿態一致性等檢查")

        if self.opts.prices_path:
            try:
                self.external_prices = load_prices(self.opts.prices_path)
            except InputError as exc:
                self.r.error("INPUT_PRICES", f"無法讀取收盤價檔：{exc}")

        if self.opts.active_etf_holdings_path:
            try:
                self.etf_holdings = load_active_etf_holdings(self.opts.active_etf_holdings_path)
            except InputError as exc:
                self.r.error("INPUT_ACTIVE_ETF", f"無法讀取主動型 ETF 持股檔：{exc}")
        else:
            self.r.skip("未提供主動型 ETF 前 15 大持股（--active-etf）：略過主動投資比率（Active Share ≥ 15%）檢查")

        if self.opts.theme_path:
            try:
                self.theme = load_theme(self.opts.theme_path)
            except InputError as exc:
                self.r.error("INPUT_THEME", f"無法讀取主題設定檔：{exc}")
        else:
            self.r.skip("未提供 ETF 主題設定（--theme）：略過主題產業占比檢查")

    # ------------------------------------------------------------------ 檔名與 schema

    def _check_filename(self) -> None:
        name = self.path.name
        m = re.fullmatch(r"D-Plan_([A-Za-z0-9_-]+)_(\d{4}-\d{2}-\d{2})\.json", name)
        if not m:
            self.r.error("FILENAME_FORMAT",
                         f"檔名 {name!r} 不符合 D-Plan_<team_id>_<trade_date>.json（字元限 A-Z a-z 0-9 _ -，副檔名小寫 .json，不可帶時間戳或 (1) 之類的後綴）",
                         consequence="後台可能拒收，或無法對應到正確的隊伍與交易日")
            return
        team, date = m.groups()
        if self.doc.get("team_id") != team:
            self.r.error("FILENAME_TEAM_MISMATCH", f"檔名中的隊伍代號 {team} 與 JSON 內 team_id {self.doc.get('team_id')!r} 不一致", consequence=REJECT)
        if self.doc.get("trade_date") != date:
            self.r.error("FILENAME_DATE_MISMATCH", f"檔名中的日期 {date} 與 JSON 內 trade_date {self.doc.get('trade_date')!r} 不一致", consequence=REJECT)

    def _check_schema(self) -> None:
        try:
            schema = json.loads(Path(self.opts.schema_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.r.error("INPUT_SCHEMA", f"無法讀取 schema：{exc}")
            return
        validator = Draft202012Validator(schema)
        for err in sorted(validator.iter_errors(self.doc), key=lambda e: [str(p) for p in e.absolute_path]):
            self.r.error("SCHEMA", self._translate_schema_error(err), _fmt_path(err.absolute_path), REJECT)

        if "_NOTE" in self.doc:
            self.r.warn("NOTE_FIELD", "含範例檔用的 _NOTE 欄位；正式提交建議移除", "_NOTE")
        version = self.doc.get("schema_version")
        if isinstance(version, str) and re.fullmatch(r"4\.\d+", version) and version != R.SCHEMA_VERSION:
            self.r.warn("SCHEMA_VERSION", f"schema_version 為 {version}，目前規格是 {R.SCHEMA_VERSION}", "schema_version")

    @staticmethod
    def _translate_schema_error(err: Any) -> str:
        v, val, inst = err.validator, err.validator_value, err.instance
        if v == "required":
            missing = [k for k in val if isinstance(inst, dict) and k not in inst]
            return f"缺少必填欄位：{', '.join(missing)}"
        if v == "additionalProperties":
            allowed = set(err.schema.get("properties", {}))
            extra = [k for k in inst if k not in allowed] if isinstance(inst, dict) else []
            return f"出現規格以外的欄位：{', '.join(extra)}（不要加規格以外的欄位）"
        if v == "type":
            return f"型別錯誤：應為 {val}，實際是 {type(inst).__name__}（值 {inst!r}）"
        if v == "pattern":
            return f"格式不符：{inst!r} 不符合規則 {val}"
        if v == "enum":
            return f"值 {inst!r} 不在允許清單 {val} 中"
        if v == "const":
            return f"值必須是 {val!r}，實際是 {inst!r}"
        if v == "minItems":
            return f"至少需要 {val} 筆，實際 {len(inst)} 筆"
        if v == "maxItems":
            return f"最多 {val} 筆，實際 {len(inst)} 筆"
        if v == "minimum":
            return f"數值 {inst} 小於下限 {val}"
        if v == "maximum":
            return f"數值 {inst} 超過上限 {val}"
        if v == "multipleOf":
            return f"數值 {inst} 不是 {val} 的倍數（委託必須是整張，1000 股的倍數）"
        if v == "minLength":
            return f"文字長度 {len(inst)} 少於下限 {val}"
        if v == "maxLength":
            return f"文字長度 {len(inst)} 超過上限 {val}"
        if v == "minProperties":
            return f"至少需要 {val} 個欄位（values 至少放一個數字）"
        if v == "uniqueItems":
            return "陣列內有重複的項目"
        return err.message

    # ------------------------------------------------------------------ 頂層與時間

    def _check_top_level(self) -> None:
        team = self.doc.get("team_id")
        if self.opts.team_id and team != self.opts.team_id:
            self.r.error("TEAM_ID_MISMATCH", f"team_id 為 {team!r}，但你們的隊伍代號是 {self.opts.team_id!r}", "team_id",
                         "繳交到錯誤隊伍或被拒收")
        elif not self.opts.team_id and team in R.EXAMPLE_TEAM_IDS:
            self.r.warn("TEAM_ID_EXAMPLE", f"team_id {team!r} 是範例檔的隊伍代號，請確認是主辦方配發給你們的代號（可用 --team-id 指定比對）", "team_id")

    def _check_times(self) -> None:
        raw_date = self.doc.get("trade_date")
        if isinstance(raw_date, str):
            try:
                self.trade_date = dt.date.fromisoformat(raw_date)
            except ValueError:
                self.r.error("TRADE_DATE_INVALID", f"trade_date {raw_date!r} 不是真實存在的日期", "trade_date", REJECT)
        td = self.trade_date
        if td:
            if td.weekday() >= 5:
                self.r.error("TRADE_DATE_WEEKEND", f"trade_date {td} 是週末，不是交易日", "trade_date")
            elif td in self.opts.holidays:
                self.r.error("TRADE_DATE_HOLIDAY", f"trade_date {td} 是休市日", "trade_date")
            if not (R.COMPETITION_START <= td <= R.COMPETITION_END):
                self.r.warn("TRADE_DATE_OUT_OF_PERIOD", f"trade_date {td} 不在初賽期間 {R.COMPETITION_START}～{R.COMPETITION_END}", "trade_date")

        meta = self.doc.get("agent_metadata") if isinstance(self.doc.get("agent_metadata"), dict) else {}
        started = self._ts(meta.get("run_started_at"), "agent_metadata.run_started_at")
        completed = self._ts(meta.get("run_completed_at"), "agent_metadata.run_completed_at")
        self.completed_at = completed
        deadline = dt.datetime.combine(td, R.SUBMIT_DEADLINE_TIME, R.TPE) if td else None
        window_open = dt.datetime.combine(td - dt.timedelta(days=1), R.SUBMIT_OPEN_TIME, R.TPE) if td else None

        if started and completed:
            if started > completed:
                self.r.error("META_TIME_ORDER", f"run_started_at（{started}）晚於 run_completed_at（{completed}）", "agent_metadata")
            elif completed - started > dt.timedelta(hours=6):
                self.r.info("META_LONG_RUN", f"Agent 執行時間 {completed - started}，偏長；請確認時間是否自報正確", "agent_metadata")
        if completed:
            if completed > self.now:
                self.r.error("META_COMPLETED_IN_FUTURE",
                             f"run_completed_at（{completed}）晚於現在（{self.now:%Y-%m-%d %H:%M:%S}）；規定它必須 ≤ 伺服器收件時間",
                             "agent_metadata.run_completed_at", "自報時間不實，資格審查時可能被質疑")
            if deadline and completed > deadline:
                self.r.error("META_AFTER_DEADLINE", f"run_completed_at（{completed}）晚於繳交截止 {deadline}",
                             "agent_metadata.run_completed_at", MISSED)
            if window_open and completed < window_open:
                self.r.warn("META_BEFORE_WINDOW",
                            f"run_completed_at（{completed}）早於繳交開放時間 {window_open}；前一交易日結算可能尚未完成，Agent 可能用了舊資料",
                            "agent_metadata.run_completed_at")

        if deadline and window_open:
            if self.now > deadline:
                self.r.warn("NOW_AFTER_DEADLINE", f"現在已超過 {td} 的繳交截止時間 {deadline:%m/%d %H:%M}", consequence=MISSED)
            elif self.now < window_open:
                self.r.info("NOW_BEFORE_WINDOW", f"{td} 的繳交時段從 {window_open:%m/%d %H:%M} 開始，現在還不能送出")

        for i, src in self._items("sources"):
            path = f"sources[{i}]"
            content = self._ts(src.get("content_as_of"), f"{path}.content_as_of")
            fetched = self._ts(src.get("fetched_at"), f"{path}.fetched_at")
            published = self._ts(src.get("published_at"), f"{path}.published_at")
            sid = src.get("source_id", path)
            if content:
                if completed and content > completed:
                    self.r.error("SOURCE_FUTURE_CONTENT", f"{sid} 的 content_as_of（{content}）晚於 Agent 完成時間（{completed}），不可能在決策時取得",
                                 f"{path}.content_as_of", "抽檢時會被認定為事後補資料")
                if deadline and content > deadline:
                    self.r.error("SOURCE_LOOKAHEAD", f"{sid} 的 content_as_of（{content}）晚於繳交截止時間，屬於使用未來資料",
                                 f"{path}.content_as_of", "抽檢時會被認定為事後補資料")
                if td and content.date() < td - dt.timedelta(days=R.SOURCE_STALE_DAYS):
                    self.r.info("SOURCE_STALE", f"{sid} 的內容時點 {content:%Y-%m-%d} 距交易日超過 {R.SOURCE_STALE_DAYS} 天，確認是否為最新資料", f"{path}.content_as_of")
            if fetched:
                if fetched > self.now:
                    self.r.error("SOURCE_FETCHED_IN_FUTURE", f"{sid} 的 fetched_at（{fetched}）晚於現在；規定須 ≤ 伺服器收件時間", f"{path}.fetched_at")
                elif completed and fetched > completed:
                    self.r.warn("SOURCE_FETCHED_AFTER_RUN", f"{sid} 的 fetched_at（{fetched}）晚於 Agent 完成時間（{completed}）", f"{path}.fetched_at")
                if content and content > fetched:
                    self.r.warn("SOURCE_CONTENT_AFTER_FETCH", f"{sid} 的 content_as_of（{content}）晚於抓取時間（{fetched}），時點描述不合理", path)
            if published and fetched and published > fetched:
                self.r.warn("SOURCE_PUBLISHED_AFTER_FETCH", f"{sid} 的 published_at（{published}）晚於抓取時間（{fetched}）", path)

    # ------------------------------------------------------------------ 編號與引用

    def _check_ids(self) -> None:
        for key, (id_key, prefix) in R.ID_PREFIX.items():
            nums: list[int] = []
            for i, item in self._items(key):
                value = item.get(id_key)
                if not isinstance(value, str):
                    continue
                m = re.fullmatch(prefix + r"(\d{1,3})", value)
                if not m:
                    continue
                digits = m.group(1)
                if digits.startswith("0"):
                    self.r.error("ID_LEADING_ZERO", f"{value} 不合規：流水號從 1 開始且不加前導零（應寫成 {prefix}{int(digits) or 1}）",
                                 f"{key}[{i}].{id_key}", REJECT)
                    continue
                nums.append(int(digits))
            counts = Counter(nums)
            dups = sorted(n for n, c in counts.items() if c > 1)
            if dups:
                self.r.error("ID_DUPLICATE", f"{key} 內有重複編號：{', '.join(prefix + str(n) for n in dups)}", key, REF_CONSEQUENCE)
            unique = sorted(counts)
            if unique and unique != list(range(1, len(unique) + 1)):
                expected = set(range(1, max(unique) + 1))
                gaps = sorted(expected - set(unique))
                detail = f"缺少 {', '.join(prefix + str(n) for n in gaps)}" if gaps else ""
                self.r.error("ID_NOT_SEQUENTIAL", f"{key} 的編號必須從 {prefix}1 開始連續不跳號；{detail}", key, REJECT)
            if nums != sorted(nums):
                self.r.warn("ID_ORDER", f"{key} 的編號沒有依序排列（{', '.join(prefix + str(n) for n in nums)}）", key)

    def _check_ref_list(self, refs: Any, valid: set[str], path: str, layer: str) -> None:
        refs_list = self._str_list(refs)
        for ref in refs_list:
            if ref not in valid:
                self.r.error("REF_BROKEN", f"引用的 {ref} 不存在於 {layer}", path, REF_CONSEQUENCE)
        dup = [k for k, c in Counter(refs_list).items() if c > 1]
        if dup:
            self.r.warn("REF_DUPLICATE", f"同一個引用欄位重複列了 {', '.join(dup)}", path)

    def _check_references(self) -> None:
        s_ids, o_ids, i_ids, d_ids = (self._ids(k) for k in ("sources", "observations", "inferences", "decisions"))
        used_s: set[str] = set()
        used_o: set[str] = set()
        used_i: set[str] = set()

        for i, obs in self._items("observations"):
            self._check_ref_list(obs.get("source_ref"), s_ids, f"observations[{i}].source_ref", "sources")
            used_s.update(self._str_list(obs.get("source_ref")))
        mv = self.doc.get("market_view")
        if isinstance(mv, dict):
            self._check_ref_list(mv.get("basis_refs"), o_ids, "market_view.basis_refs", "observations")
            used_o.update(self._str_list(mv.get("basis_refs")))
        for i, inf in self._items("inferences"):
            self._check_ref_list(inf.get("premise_refs"), o_ids, f"inferences[{i}].premise_refs", "observations")
            used_o.update(self._str_list(inf.get("premise_refs")))
        for i, dec in self._decisions():
            self._check_ref_list(dec.get("inference_refs"), i_ids, f"decisions[{i}].inference_refs", "inferences")
            used_i.update(self._str_list(dec.get("inference_refs")))
        for i, nt in self._items("no_trade_decisions"):
            self._check_ref_list(nt.get("reason_refs"), i_ids, f"no_trade_decisions[{i}].reason_refs", "inferences")
            used_i.update(self._str_list(nt.get("reason_refs")))
        for i, order in self._items("orders"):
            ref = order.get("decision_ref")
            if isinstance(ref, str) and ref not in d_ids:
                self.r.error("REF_BROKEN", f"委託引用的決策 {ref} 不存在於 decisions", f"orders[{i}].decision_ref", REF_CONSEQUENCE)

        for sid in sorted(s_ids - used_s, key=lambda x: int(x[1:]) if x[1:].isdigit() else 0):
            self.r.warn("SOURCE_UNUSED", f"資料來源 {sid} 沒有被任何 observation 引用（sources 只列實際用到的來源）", "sources")
        for oid in sorted(o_ids - used_o, key=lambda x: int(x[1:]) if x[1:].isdigit() else 0):
            self.r.warn("OBS_UNUSED", f"事實 {oid} 沒有被 market_view 或任何推論引用", "observations")
        for iid in sorted(i_ids - used_i, key=lambda x: int(x[1:]) if x[1:].isdigit() else 0):
            self.r.warn("INF_UNUSED", f"推論 {iid} 沒有被任何決策或續抱理由引用", "inferences")

    def _check_mentions(self, text: Any, allowed_obs: set[str], path: str) -> None:
        """文字中提到的 O/I/D 編號應存在，提到的事實也應列在引用欄位中。"""
        if not isinstance(text, str):
            return
        existing = {"O": self._ids("observations"), "I": self._ids("inferences"), "D": self._ids("decisions"), "S": self._ids("sources")}
        for prefix, num in ID_MENTION.findall(text):
            ident = f"{prefix}{num}"
            if ident not in existing[prefix]:
                self.r.warn("TEXT_MENTIONS_MISSING_ID", f"文字中提到 {ident}，但它不存在", path)
            elif prefix == "O" and ident not in allowed_obs:
                self.r.warn("TEXT_MENTIONS_UNREFERENCED_OBS", f"文字中提到 {ident}，但沒有列在引用欄位裡", path)

    # ------------------------------------------------------------------ 各層內容

    def _check_sources(self) -> None:
        seen_urls: dict[str, str] = {}
        for i, src in self._items("sources"):
            path = f"sources[{i}]"
            sid = src.get("source_id", path)
            authority = src.get("authority")
            url = src.get("url")
            host = ""
            if isinstance(url, str):
                parsed = urlparse(url)
                host = (parsed.hostname or "").lower()
                if parsed.scheme not in ("http", "https") or not host:
                    self.r.error("SOURCE_URL_INVALID", f"{sid} 的 url {url!r} 不是有效的 http(s) 網址", f"{path}.url", REJECT)
                elif any(host == h or host.endswith("." + h) for h in R.PLACEHOLDER_URL_HOSTS):
                    self.r.warn("SOURCE_URL_PLACEHOLDER", f"{sid} 的網址 {url} 是範例或本機網址，抽檢時無法核對", f"{path}.url")
                if url in seen_urls:
                    self.r.info("SOURCE_URL_DUPLICATE", f"{sid} 與 {seen_urls[url]} 的網址相同，可考慮合併", f"{path}.url")
                seen_urls.setdefault(url, str(sid))

            archive = src.get("archive_url")
            if isinstance(archive, str):
                parsed = urlparse(archive)
                ahost = (parsed.hostname or "").lower()
                if parsed.scheme not in ("http", "https") or not ahost:
                    self.r.error("SOURCE_ARCHIVE_INVALID", f"{sid} 的 archive_url {archive!r} 不是有效網址", f"{path}.archive_url", REJECT)
                elif not any(ahost == d or ahost.endswith("." + d) for d in R.ARCHIVE_DOMAINS):
                    self.r.info("SOURCE_ARCHIVE_DOMAIN", f"{sid} 的 archive_url 不是 web.archive.org / archive.today，抽檢可能不採信", f"{path}.archive_url")
            elif authority in R.ARCHIVE_RECOMMENDED_AUTHORITIES:
                self.r.warn("SOURCE_NO_ARCHIVE", f"{sid}（{authority}）沒有 archive_url；原始網頁可能事後變動，存檔是唯一佐證",
                            f"{path}.archive_url")

            if host and isinstance(authority, str):
                matched = next((a for a, doms in R.AUTHORITY_DOMAINS.items()
                                if any(host == d or host.endswith("." + d) for d in doms)), None)
                if matched and matched != authority:
                    self.r.warn("SOURCE_AUTHORITY_MISMATCH", f"{sid} 的網址屬於 {matched}，但 authority 填 {authority}", f"{path}.authority")
                elif authority in R.AUTHORITY_DOMAINS and not matched:
                    self.r.warn("SOURCE_AUTHORITY_DOMAIN", f"{sid} 的 authority 是 {authority}，但網址 {host} 不是該機構的官方網域", f"{path}.authority")

    @staticmethod
    def _number_forms(value: float) -> set[str]:
        forms = {f"{value:g}", f"{abs(value):g}"}
        if float(value).is_integer():
            forms |= {str(int(value)), str(abs(int(value))), f"{abs(int(value)):,}"}
        else:
            forms |= {f"{abs(value):,}", repr(abs(value)), f"{abs(value):,.2f}"}
        return forms

    def _check_observations(self) -> None:
        for i, obs in self._items("observations"):
            path = f"observations[{i}]"
            statement = obs.get("statement")
            if not isinstance(statement, str):
                continue
            words = [w for w in R.INFERENCE_WORDS if w in statement]
            if words:
                self.r.warn("OBS_HAS_INFERENCE", f"{obs.get('obs_id')} 的 statement 含推論性用語（{'、'.join(words)}）；事實層只寫可核對的事實",
                            f"{path}.statement")
            values = obs.get("values")
            if isinstance(values, dict) and values:
                numeric = [v for v in values.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
                if numeric and not any(form in statement for v in numeric for form in self._number_forms(v)):
                    self.r.info("OBS_VALUES_NOT_IN_TEXT", f"{obs.get('obs_id')} 的 values 數字都沒有出現在 statement 中，請確認兩者一致", path)

    def _check_market_view(self) -> None:
        mv = self.doc.get("market_view")
        if not isinstance(mv, dict):
            return
        posture = mv.get("posture") if isinstance(mv.get("posture"), dict) else {}
        rng = posture.get("target_cash_pct_range")
        if isinstance(rng, list) and len(rng) == 2 and all(isinstance(x, (int, float)) for x in rng):
            lo, hi = rng
            if lo > hi:
                self.r.error("CASH_RANGE_INVERTED", f"target_cash_pct_range 下限 {lo} 大於上限 {hi}", "market_view.posture.target_cash_pct_range", REJECT)
            if hi > R.CASH_MAX - R.CASH_EDGE_BUFFER:
                self.r.warn("CASH_RANGE_NEAR_CAP", f"現金區間上限 {_pct(hi)} 太接近規則上限 25%；實際以當日均價成交，可能超限",
                            "market_view.posture.target_cash_pct_range")
            if lo < R.CASH_EDGE_BUFFER:
                self.r.warn("CASH_RANGE_NEAR_ZERO", f"現金區間下限 {_pct(lo)} 太接近 0%；實際成交價若較高，現金可能變負數",
                            "market_view.posture.target_cash_pct_range")
        regime, stance = mv.get("regime"), mv.get("stance")
        intent = posture.get("net_exposure_intent")
        if (regime, stance) in R.REGIME_STANCE_CONFLICT:
            self.r.info("REGIME_STANCE_CONFLICT", f"regime={regime} 與 stance={stance} 方向相反；規則允許，但 logic 應交代原因", "market_view")
        if (stance, intent) in R.STANCE_INTENT_CONFLICT:
            self.r.warn("STANCE_INTENT_CONFLICT", f"stance={stance} 卻宣告 net_exposure_intent={intent}，敘述與部位意圖矛盾", "market_view")
        if mv.get("counter_evidence") == "":
            self.r.warn("EMPTY_COUNTER_EVIDENCE", "counter_evidence 為空字串；沒有反方訊號請填 null", "market_view.counter_evidence")
        self._check_mentions(mv.get("logic"), set(self._str_list(mv.get("basis_refs"))), "market_view.logic")

    def _check_inferences(self) -> None:
        logics: dict[str, str] = {}
        for i, inf in self._items("inferences"):
            path = f"inferences[{i}]"
            self._check_mentions(inf.get("logic"), set(self._str_list(inf.get("premise_refs"))), f"{path}.logic")
            if inf.get("counter_evidence") == "":
                self.r.warn("EMPTY_COUNTER_EVIDENCE", "counter_evidence 為空字串；沒有反方訊號請填 null", f"{path}.counter_evidence")
            logic = inf.get("logic")
            if isinstance(logic, str):
                if logic in logics:
                    self.r.info("INF_DUPLICATE_LOGIC", f"{inf.get('inf_id')} 的 logic 與 {logics[logic]} 完全相同", path)
                logics.setdefault(logic, str(inf.get("inf_id")))

    def _check_leftover_text(self) -> None:
        def walk(node: Any, path: str) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    if k != "_NOTE":
                        walk(v, f"{path}.{k}" if path else k)
            elif isinstance(node, list):
                for idx, v in enumerate(node):
                    walk(v, f"{path}[{idx}]")
            elif isinstance(node, str):
                hits = [m for m in R.LEFTOVER_MARKERS if m.lower() in node.lower()]
                if hits:
                    self.r.warn("TEXT_LEFTOVER", f"文字含疑似未完成或模型殘留的內容：{', '.join(hits)}", path)
        walk(self.doc, "")

    def _check_metadata(self) -> None:
        meta = self.doc.get("agent_metadata")
        if not isinstance(meta, dict):
            return
        if meta.get("code_version") in R.EXAMPLE_CODE_VERSIONS:
            self.r.warn("META_CODE_VERSION_EXAMPLE", f"code_version {meta.get('code_version')!r} 是範例值；必須對應當天實際執行的程式版本（晉級後要能拿出這一版）",
                        "agent_metadata.code_version")
        if meta.get("model_provider") == "other" and isinstance(meta.get("model_version"), str) and len(meta["model_version"]) < 6:
            self.r.info("META_MODEL_OTHER", "model_provider 為 other 時，model_version 要寫明是哪一個模型", "agent_metadata.model_version")

    # ------------------------------------------------------------------ 決策與委託（不需資料包）

    def _check_decisions_static(self) -> None:
        by_id = self._decision_by_id()
        dec_tickers = [d.get("ticker") for _, d in self._decisions() if isinstance(d.get("ticker"), str)]
        nt_tickers = [n.get("ticker") for _, n in self._items("no_trade_decisions") if isinstance(n.get("ticker"), str)]
        for t, c in Counter(dec_tickers).items():
            if c > 1:
                self.r.error("DECISION_DUP_TICKER", f"{self._name(t)} 在 decisions 出現 {c} 次；同一檔只能有一筆決策", "decisions", COVERAGE_CONSEQUENCE)
        for t, c in Counter(nt_tickers).items():
            if c > 1:
                self.r.error("NO_TRADE_DUP_TICKER", f"{self._name(t)} 在 no_trade_decisions 出現 {c} 次", "no_trade_decisions", COVERAGE_CONSEQUENCE)
        for t in sorted(set(dec_tickers) & set(nt_tickers)):
            self.r.error("DECISION_AND_NO_TRADE", f"{self._name(t)} 同時出現在 decisions 與 no_trade_decisions，只能擇一", "decisions", COVERAGE_CONSEQUENCE)

        inferences = {x.get("inf_id"): x for _, x in self._items("inferences")}
        for i, dec in self._decisions():
            path = f"decisions[{i}]"
            ticker, action, tw = dec.get("ticker"), dec.get("action"), dec.get("target_weight")
            if not isinstance(ticker, str) or not isinstance(tw, (int, float)) or isinstance(tw, bool):
                continue
            cap = R.weight_cap(ticker)
            if action in R.BUY_ACTIONS:
                if tw > cap:
                    self.r.error("DECISION_OVER_CAP", f"{self._name(ticker)} {action} 目標權重 {_pct(tw)} 超過個股上限 {_pct(cap)}",
                                 f"{path}.target_weight", VIOLATION)
                elif tw > cap - R.WEIGHT_CAP_BUFFER:
                    self.r.warn("DECISION_NEAR_CAP", f"{self._name(ticker)} 目標權重 {_pct(tw)} 距上限 {_pct(cap)} 不到 {_pct(R.WEIGHT_CAP_BUFFER)}；當日價格上漲可能讓加碼部位超限（加碼造成的超限當日即記警告）",
                                f"{path}.target_weight")
            elif action == "TRIM" and tw > cap:
                self.r.warn("TRIM_STILL_OVER_CAP", f"{self._name(ticker)} 減碼後目標權重 {_pct(tw)} 仍超過上限 {_pct(cap)}；被動超限須在 5 個交易日內降到上限以下",
                            f"{path}.target_weight")
            if action in ("BUY", "ADD", "TRIM") and tw == 0:
                hint = "出清請用 SELL_ALL" if action == "TRIM" else "目標權重為 0 不會產生買進"
                self.r.error("DECISION_ZERO_WEIGHT", f"{self._name(ticker)} 的 {action} 目標權重為 0；{hint}", f"{path}.target_weight", SEVERE)
            if action == "SELL_ALL" and tw != 0:
                self.r.warn("SELL_ALL_NONZERO_WEIGHT", f"{self._name(ticker)} 為 SELL_ALL，但 target_weight 是 {tw}；出清應填 0", f"{path}.target_weight")

            if not self._decision_mentions_ticker(dec.get("inference_refs"), inferences, ticker):
                self.r.info("DECISION_INF_NO_TICKER", f"{self._name(ticker)} 引用的推論中沒有提到這檔股票（代號或名稱），請確認推論與決策對得上",
                            f"{path}.inference_refs")

            funding = dec.get("funding_for")
            if funding is None:
                continue
            if action not in R.SELL_ACTIONS:
                self.r.error("FUNDING_ON_NON_SELL", f"funding_for 只能用在 TRIM / SELL_ALL，{dec.get('decision_id')} 是 {action}", f"{path}.funding_for", SEVERE)
            for ref in self._str_list(funding):
                target = by_id.get(ref)
                if ref == dec.get("decision_id"):
                    self.r.error("FUNDING_SELF", f"{ref} 的 funding_for 指向自己", f"{path}.funding_for", SEVERE)
                elif target is None:
                    self.r.error("REF_BROKEN", f"funding_for 引用的決策 {ref} 不存在", f"{path}.funding_for", REF_CONSEQUENCE)
                elif target.get("action") not in R.BUY_ACTIONS:
                    self.r.error("FUNDING_TARGET_NOT_BUY", f"funding_for 指向的 {ref} 是 {target.get('action')}，必須是同日的 BUY/ADD 決策",
                                 f"{path}.funding_for", SEVERE)
            logic_text = " ".join(str(inferences.get(r, {}).get("logic", "")) for r in self._str_list(dec.get("inference_refs")))
            if logic_text and not any(w in logic_text for w in R.FUNDING_LOGIC_WORDS):
                self.r.warn("FUNDING_NO_SELECTION_LOGIC", f"{dec.get('decision_id')} 標了 funding_for，但引用的推論沒有說明「為何選這檔讓位」",
                            f"{path}.inference_refs")

        for i, nt in self._items("no_trade_decisions"):
            ticker = nt.get("ticker")
            if isinstance(ticker, str) and not self._decision_mentions_ticker(nt.get("reason_refs"), inferences, ticker):
                self.r.info("NO_TRADE_INF_NO_TICKER", f"{self._name(ticker)} 續抱引用的推論中沒有提到這檔股票，理由可能對不上",
                            f"no_trade_decisions[{i}].reason_refs")

    def _decision_mentions_ticker(self, refs: Any, inferences: dict[Any, dict[str, Any]], ticker: str) -> bool:
        name = self.universe.get(ticker, "")
        texts = []
        for ref in self._str_list(refs):
            inf = inferences.get(ref)
            if inf:
                texts.append(f"{inf.get('logic', '')} {inf.get('conclusion', '')}")
        if not texts:
            return True
        blob = " ".join(texts)
        return ticker in blob or bool(name and name in blob)

    def _check_orders_static(self) -> None:
        by_id = self._decision_by_id()
        per_decision: Counter[str] = Counter()
        tickers: Counter[str] = Counter()
        for i, order in self._items("orders"):
            path = f"orders[{i}]"
            ticker, side, shares, ref = order.get("ticker"), order.get("side"), order.get("shares"), order.get("decision_ref")
            if isinstance(shares, float):
                self.r.warn("ORDER_SHARES_FLOAT", f"shares 寫成小數 {shares}；請輸出整數", f"{path}.shares")
            if isinstance(ticker, str):
                tickers[ticker] += 1
            if isinstance(ref, str):
                per_decision[ref] += 1
            dec = by_id.get(ref) if isinstance(ref, str) else None
            if not dec:
                continue
            if isinstance(ticker, str) and dec.get("ticker") != ticker:
                self.r.error("ORDER_TICKER_MISMATCH", f"委託 {ticker} 引用的 {ref} 是 {dec.get('ticker')} 的決策", path, SEVERE)
            action = dec.get("action")
            if side == "BUY" and action in R.SELL_ACTIONS or side == "SELL" and action in R.BUY_ACTIONS:
                self.r.error("ORDER_SIDE_MISMATCH", f"{ref} 的動作是 {action}，委託方向卻是 {side}", f"{path}.side", SEVERE)
        for t, c in tickers.items():
            if c > 1:
                self.r.error("ORDER_DUP_TICKER", f"{self._name(t)} 有 {c} 筆委託；每檔一天只應有一筆", "orders", SEVERE)
        for ref, c in per_decision.items():
            if c > 1:
                self.r.error("ORDER_DUP_DECISION", f"決策 {ref} 對應了 {c} 筆委託；官方演算法每筆決策只會算出一筆", "orders", SEVERE)

    def _check_universe(self) -> None:
        if not self.universe:
            return
        for key in ("decisions", "no_trade_decisions", "orders"):
            for i, item in self._items(key):
                ticker = item.get("ticker")
                if isinstance(ticker, str) and re.fullmatch(r"\d{4}", ticker) and ticker not in self.universe:
                    self.r.error("TICKER_NOT_IN_UNIVERSE", f"{ticker} 不在 150 檔標的清單內", f"{key}[{i}].ticker", VIOLATION)

    # ------------------------------------------------------------------ 資料包與部位

    def _prepare_portfolio(self) -> bool:
        dp = self.datapack
        if dp is None:
            return False
        if dp.get("doc_type") != "DataPack":
            self.r.error("DATAPACK_TYPE", f"資料包 doc_type 為 {dp.get('doc_type')!r}，不是 DataPack；是否放錯檔案？")
        if dp.get("team_id") != self.doc.get("team_id"):
            self.r.error("DATAPACK_TEAM_MISMATCH", f"資料包隊伍 {dp.get('team_id')!r} 與 D-Plan team_id {self.doc.get('team_id')!r} 不一致")
        if dp.get("trade_date") != self.doc.get("trade_date"):
            self.r.error("DATAPACK_DATE_MISMATCH",
                         f"資料包對應交易日 {dp.get('trade_date')}，D-Plan 是 {self.doc.get('trade_date')}；持股與淨值基準不同，請重新下載當日資料包",
                         consequence="委託推導、覆蓋與超賣判斷都會以錯的部位為準")

        compliance = dp.get("compliance") if isinstance(dp.get("compliance"), dict) else {}
        violations = compliance.get("violations", 0)
        if isinstance(violations, int):
            if violations >= R.MAX_VIOLATIONS:
                self.r.error("VIOLATIONS_EXHAUSTED", f"資料包顯示已累計 {violations} 次違規警告，達取消資格門檻", consequence=DISQUALIFY)
            elif violations == R.MAX_VIOLATIONS - 1:
                self.r.warn("VIOLATIONS_LAST_CHANCE", f"已累計 {violations} 次違規警告；再一次即取消資格，今天務必保守", consequence=DISQUALIFY)
            elif violations > 0:
                self.r.info("VIOLATIONS_SO_FAR", f"目前累計 {violations} 次違規警告（{R.MAX_VIOLATIONS} 次取消資格）")

        prev = dp.get("prev_day_orders") if isinstance(dp.get("prev_day_orders"), dict) else {}
        if prev.get("outcome") == "voided":
            self.r.warn("PREV_DAY_VOIDED", f"前一交易日委託整批作廢，原因：{', '.join(prev.get('void_reasons') or ['未說明'])}；請確認今天不會重犯")
        elif prev.get("outcome") == "none":
            self.r.info("PREV_DAY_NONE", "前一交易日沒有繳交委託")
        if prev.get("excluded_codes"):
            self.r.info("PREV_DAY_EXCLUDED", f"前一交易日有標的因缺成交均價被排除：{', '.join(prev['excluded_codes'])}")

        missing = dp.get("missing") or []
        if "prev_settlement" in missing or dp.get("account") is None or dp.get("holdings") is None:
            self.r.error("DATAPACK_NOT_SETTLED", "資料包的前一交易日結算尚未產生（account / holdings 為 null）；請等結算完成後重新下載再產生 D-Plan",
                         consequence="無法確認持股，極易超賣或決策覆蓋不完整")
            self.r.skip("資料包尚未結算：略過所有部位相關檢查")
            return False
        if "prev_close" in missing:
            self.r.warn("DATAPACK_NO_PREV_CLOSE", "資料包註明收盤價尚未匯入；後台檢核可能把部分項目標為未驗，建議稍後重新下載")

        account = dp["account"]
        positions: dict[str, Position] = {}
        for h in dp["holdings"]:
            code = str(h.get("code"))
            positions[code] = Position(code, int(h.get("shares") or 0), int(h.get("receivable_shares") or 0),
                                       h.get("close_price"), h.get("tradable_weight"))
        try:
            self.book = Book(float(account["cash"]), float(account["nav_tradable"]), positions)
        except (KeyError, TypeError, ValueError) as exc:
            self.r.error("DATAPACK_ACCOUNT", f"資料包 account 欄位不完整：{exc}")
            return False

        for code, pos in positions.items():
            ext = self.external_prices.get(code)
            if pos.close_price is not None:
                self.price_of[code] = float(pos.close_price)
                if ext is not None and abs(ext - pos.close_price) / pos.close_price > 0.001:
                    self.r.warn("PRICE_MISMATCH", f"{self._name(code)} 收盤價：資料包 {pos.close_price}、價格檔 {ext}，以資料包為準")
            elif pos.shares > 0:
                self.r.warn("DATAPACK_NO_CLOSE", f"{self._name(code)} 在資料包中沒有收盤價，系統以 0 計入淨值")
            if pos.shares % R.LOT_SIZE:
                self.r.warn("HOLDING_ODD_LOT", f"{self._name(code)} 持有 {pos.shares:,} 股，不是整張；可能無法以整股方式全部賣出")
        for code, price in self.external_prices.items():
            self.price_of.setdefault(code, price)

        equity = sum(p.shares * self.price_of.get(c, 0.0) for c, p in positions.items() if p.shares > 0)
        if abs(self.book.cash + equity - self.book.nav) > 1:
            self.r.warn("DATAPACK_NAV_INCONSISTENT", f"資料包 nav_tradable {_money(self.book.nav)} 與 cash＋持股市值 {_money(self.book.cash + equity)} 不一致")
        aum = account.get("aum")
        if isinstance(aum, (int, float)) and aum != self.book.nav:
            self.r.info("AUM_VS_NAV", f"aum {_money(aum)} ≠ nav_tradable {_money(self.book.nav)}（差額為應收配息／配股）；推導與比率一律用 nav_tradable")

        generated = self._ts(dp.get("generated_at"), "datapack.generated_at")
        if generated and self.completed_at and self.completed_at < generated:
            self.r.warn("RUN_BEFORE_DATAPACK", f"Agent 完成時間 {self.completed_at} 早於資料包產生時間 {generated}；D-Plan 可能不是用這份資料包產生的")

        self.r.summary["前日可交易淨值（nav_tradable）"] = _money(self.book.nav)
        self.r.summary["前日現金"] = f"{_money(self.book.cash)}（{_pct(self.book.cash / self.book.nav)}）" if self.book.nav else _money(self.book.cash)
        self.r.summary["前日持股檔數"] = len(self.book.held_codes)
        return True

    def _check_coverage(self) -> None:
        assert self.book
        held = self.book.held_codes
        dec = {d.get("ticker") for _, d in self._decisions()}
        nt = {n.get("ticker") for _, n in self._items("no_trade_decisions")}
        for code in sorted(held - dec - nt):
            self.r.error("COVERAGE_MISSING", f"昨日持股 {self._name(code)} 沒有出現在 decisions 或 no_trade_decisions", "no_trade_decisions",
                         COVERAGE_CONSEQUENCE)
        for i, item in self._items("no_trade_decisions"):
            code = item.get("ticker")
            if isinstance(code, str) and code not in held:
                pos = self.book.positions.get(code)
                extra = "（只有應收配股，庫存為 0，不算持股）" if pos and pos.receivable_shares else ""
                self.r.error("NO_TRADE_NOT_HELD", f"{self._name(code)} 昨日沒有持股{extra}，不應列在 no_trade_decisions",
                             f"no_trade_decisions[{i}].ticker", COVERAGE_CONSEQUENCE)
        for i, d in self._decisions():
            code, action = d.get("ticker"), d.get("action")
            if not isinstance(code, str):
                continue
            shares = self.book.shares(code)
            if action == "BUY" and shares > 0:
                self.r.error("ACTION_BUY_BUT_HELD", f"{self._name(code)} 昨日已持有 {shares:,} 股，動作應為 ADD 而不是 BUY", f"decisions[{i}].action", SEVERE)
            elif action in ("ADD", "TRIM", "SELL_ALL") and shares == 0:
                hint = "新建倉應用 BUY" if action == "ADD" else "沒有持股不能賣出"
                self.r.error("ACTION_NOT_HELD", f"{self._name(code)} 昨日沒有持股（庫存 0 股），{action} 不成立；{hint}", f"decisions[{i}].action",
                             SEVERE if action == "ADD" else VIOLATION)

    def _check_derivation(self) -> None:
        assert self.book
        nav = self.book.nav
        orders_by_ref: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for i, o in self._items("orders"):
            if isinstance(o.get("decision_ref"), str):
                orders_by_ref.setdefault(o["decision_ref"], []).append((i, o))

        for i, d in self._decisions():
            path = f"decisions[{i}]"
            did, code, action, tw = d.get("decision_id"), d.get("ticker"), d.get("action"), d.get("target_weight")
            if not isinstance(code, str) or not isinstance(tw, (int, float)) or isinstance(tw, bool):
                continue
            price = self.price_of.get(code)
            if price is None:
                self.r.error("PRICE_MISSING", f"沒有 {self._name(code)} 的前日收盤價，無法驗證委託股數；請用 --prices 提供", path)
                continue
            exact, floating = target_shares(float(tw), nav, price)
            if exact != floating:
                self.r.warn("DERIVATION_FLOAT_EDGE",
                            f"{self._name(code)} 的 target_weight 剛好落在整張邊界：精確計算得 {exact:,} 股、浮點計算得 {floating:,} 股，伺服器可能算出不同結果；建議微調 target_weight",
                            f"{path}.target_weight")
            current = self.book.shares(code)
            delta = exact - current
            formula = f"floor({tw} × {_money(nav)} ÷ {price} ÷ 1000) × 1000 = {exact:,}；現持 {current:,}"

            if delta and delta % R.LOT_SIZE:
                self.r.error("DERIVATION_ODD_LOT", f"{self._name(code)} 現持 {current:,} 股不是整張，推導出的委託 {abs(delta):,} 股不是 1000 的倍數", path,
                             "委託非整股，當日整批作廢並記警告（dplan_round_lot）")
            if action == "BUY" and exact == 0:
                self.r.error("BUY_ZERO_SHARES", f"{self._name(code)} BUY 推導出 0 股（{formula}）；目標權重買不到一張", path, SEVERE)
            elif action == "ADD" and delta <= 0:
                self.r.error("ACTION_DERIVATION_MISMATCH", f"{self._name(code)} 為 ADD，但推導結果是{'賣出' if delta < 0 else '不動'}（{formula}）", path, SEVERE)
            elif action == "TRIM" and exact == 0:
                self.r.error("ACTION_DERIVATION_MISMATCH", f"{self._name(code)} 為 TRIM，但推導後持股為 0；全部賣出請用 SELL_ALL（{formula}）", path, SEVERE)
            elif action == "TRIM" and delta >= 0:
                self.r.error("ACTION_DERIVATION_MISMATCH", f"{self._name(code)} 為 TRIM，但推導結果是{'買進' if delta > 0 else '不動'}（{formula}）", path, SEVERE)
            elif action == "SELL_ALL" and exact != 0:
                self.r.error("ACTION_DERIVATION_MISMATCH", f"{self._name(code)} 為 SELL_ALL，但推導後仍持有 {exact:,} 股（{formula}）", path, SEVERE)
            if delta == 0 and action != "BUY":
                self.r.warn("DECISION_NO_ORDER", f"{self._name(code)} 的決策推導不出任何委託（{formula}）；不調整的持股應列在 no_trade_decisions", path)

            expected = None if delta == 0 else ("BUY" if delta > 0 else "SELL", abs(delta))
            actual = orders_by_ref.get(did, []) if isinstance(did, str) else []
            if expected and not actual:
                self.r.error("ORDER_MISSING", f"{did}（{self._name(code)}）應產生委託 {expected[0]} {expected[1]:,} 股，但 orders 沒有對應項目（{formula}）",
                             "orders", SEVERE)
            elif not expected and actual:
                self.r.error("ORDER_UNEXPECTED", f"{did}（{self._name(code)}）推導結果不需委託，但 orders 有 {len(actual)} 筆（{formula}）", "orders", SEVERE)
            elif expected:
                for j, o in actual:
                    if (o.get("side"), o.get("shares")) != expected or o.get("ticker") != code:
                        self.r.error("ORDER_DERIVATION_MISMATCH",
                                     f"{did}（{self._name(code)}）的委託寫 {o.get('side')} {o.get('shares')} 股，官方公式應為 {expected[0]} {expected[1]:,} 股（{formula}）",
                                     f"orders[{j}]", SEVERE)

        for i, o in self._items("orders"):
            code, side, shares = o.get("ticker"), o.get("side"), o.get("shares")
            if side == "SELL" and isinstance(code, str) and isinstance(shares, (int, float)):
                held = self.book.shares(code)
                if shares > held:
                    self.r.error("OVERSELL", f"賣出 {self._name(code)} {int(shares):,} 股，但庫存只有 {held:,} 股（應收配股不能賣）", f"orders[{i}]", VIOLATION)
            if isinstance(code, str) and code not in self.price_of:
                self.r.warn("ORDER_NO_PRICE", f"{self._name(code)} 沒有前日收盤價，下列現金與權重估算未納入這筆委託", f"orders[{i}]")

    def _valid_orders(self) -> list[Order]:
        out = []
        for _, o in self._items("orders"):
            code, side, shares = o.get("ticker"), o.get("side"), o.get("shares")
            if isinstance(code, str) and side in ("BUY", "SELL") and isinstance(shares, (int, float)) and not isinstance(shares, bool) and shares > 0:
                out.append(Order(code, side, int(shares), o.get("decision_ref")))
        return out

    def _check_post_trade(self) -> None:
        assert self.book
        book, nav = self.book, self.book.nav
        orders = self._valid_orders()
        sim = simulate(book, orders, self.price_of)
        mv = self.doc.get("market_view") if isinstance(self.doc.get("market_view"), dict) else {}
        posture = mv.get("posture") if isinstance(mv.get("posture"), dict) else {}

        s = self.r.summary
        s["買進金額（前日收盤價估）"] = _money(sim.buy_amount)
        s["賣出金額（前日收盤價估）"] = _money(sim.sell_amount)
        s["淨買超"] = f"{_money(sim.net_flow)}（占淨值 {_pct(sim.net_flow / nav)}）"
        s["預估手續費＋證交稅"] = _money(sim.fees)
        s["交易後現金比（不含費用 / 含費用）"] = f"{_pct(sim.cash_ratio_gross)} / {_pct(sim.cash_ratio)}"
        s["交易後持股檔數"] = sim.holdings_count
        tolerance = sim.adverse_move_tolerance(book.cash)
        s["現金歸零前可承受的不利價格變動"] = "不受影響" if math.isinf(tolerance) else _pct(tolerance)
        turnover = (sim.buy_amount + sim.sell_amount) / nav if nav else 0.0
        s["當日周轉率"] = _pct(turnover)

        if sim.cash_after < 0:
            self.r.error("CASH_NEGATIVE", f"交易後現金預估為 {_money(sim.cash_after)}（含費用），結算現金不可為負", "orders", VIOLATION)
        elif sim.cash_ratio > R.CASH_MAX:
            self.r.error("CASH_OVER_LIMIT", f"交易後現金比預估 {_pct(sim.cash_ratio)}，超過 25% 上限", "orders", VIOLATION)
        elif sim.cash_ratio < R.CASH_EDGE_BUFFER or sim.cash_ratio > R.CASH_MAX - R.CASH_EDGE_BUFFER:
            self.r.warn("CASH_NEAR_LIMIT", f"交易後現金比預估 {_pct(sim.cash_ratio)}，離規則邊界（0%／25%）不到 {_pct(R.CASH_EDGE_BUFFER)}；實際以當日均價成交，可能越界",
                        "orders")
        if sim.cash_after >= 0:
            stressed = simulate(book, orders, self.price_of, buy_move=R.STRESS_PRICE_MOVE, sell_move=-R.STRESS_PRICE_MOVE)
            if stressed.cash_after < 0:
                self.r.warn("CASH_STRESS_NEGATIVE", f"若買進均價高 {_pct(R.STRESS_PRICE_MOVE)}、賣出均價低 {_pct(R.STRESS_PRICE_MOVE)}，現金會變成 {_money(stressed.cash_after)}",
                            "orders", VIOLATION)
        if sim.cash_ratio <= R.CASH_MAX:
            relaxed = simulate(book, orders, self.price_of, buy_move=-R.STRESS_PRICE_MOVE, sell_move=R.STRESS_PRICE_MOVE)
            if relaxed.cash_ratio > R.CASH_MAX:
                self.r.warn("CASH_STRESS_OVER", f"若買進均價低 {_pct(R.STRESS_PRICE_MOVE)}、賣出均價高 {_pct(R.STRESS_PRICE_MOVE)}，現金比會升到 {_pct(relaxed.cash_ratio)}",
                            "orders", VIOLATION)

        self._check_posture(sim, posture, nav)
        self._check_funding(book, orders, posture)

        count = sim.holdings_count
        if not R.MIN_HOLDINGS <= count <= R.MAX_HOLDINGS:
            self.r.error("HOLDINGS_COUNT", f"交易後持股 {count} 檔，必須介於 {R.MIN_HOLDINGS}–{R.MAX_HOLDINGS} 檔", "decisions", VIOLATION)
        elif count in (R.MIN_HOLDINGS, R.MAX_HOLDINGS):
            self.r.info("HOLDINGS_COUNT_EDGE", f"交易後持股 {count} 檔，剛好在邊界；若有買進標的因缺成交均價被排除，檔數可能越界")

        self._check_weights(sim)

        for code in sorted(c for c, sh in sim.shares_after.items() if sh > 0):
            if self.universe and code not in self.universe:
                self.r.error("HOLDING_NOT_IN_UNIVERSE", f"交易後仍持有不在 150 檔清單內的 {code}", consequence=VIOLATION)

        if turnover > 0.3:
            self.r.info("HIGH_TURNOVER", f"當日周轉率 {_pct(turnover)}，交易成本約 {_money(sim.fees)}；初賽只有 24 個交易日，頻繁換股會侵蝕報酬")

        weights = {c: sim.weight(c) for c, sh in sim.shares_after.items() if sh > 0}
        if weights:
            top = max(weights.items(), key=lambda kv: kv[1])
            s["交易後最大權重"] = f"{self._name(top[0])} {_pct(top[1])}"
        self._check_active_share(weights)
        self._check_theme(weights)

    def _check_posture(self, sim: Any, posture: dict[str, Any], nav: float) -> None:
        intent = posture.get("net_exposure_intent")
        net = sim.net_flow
        if intent == "increase" and not net > 0:
            self.r.error("POSTURE_NET_FLOW", f"宣告 increase（淨買超），但委託淨流向為 {_money(net)}", "market_view.posture.net_exposure_intent", SEVERE)
        elif intent == "reduce" and not net < 0:
            self.r.error("POSTURE_NET_FLOW", f"宣告 reduce（淨賣超），但委託淨流向為 {_money(net)}", "market_view.posture.net_exposure_intent", SEVERE)
        elif intent == "hold":
            ratio = abs(net) / nav if nav else 0.0
            if ratio > R.HOLD_NET_FLOW_TOLERANCE:
                self.r.error("POSTURE_NET_FLOW", f"宣告 hold，但淨買賣差 {_money(net)} 占淨值 {_pct(ratio)}，超過 2% 容差",
                             "market_view.posture.net_exposure_intent", SEVERE)
            elif ratio > R.HOLD_NET_FLOW_WARN:
                self.r.warn("POSTURE_NET_FLOW_EDGE", f"宣告 hold，淨買賣差占淨值 {_pct(ratio)}，接近 2% 容差", "market_view.posture.net_exposure_intent")

        rng = posture.get("target_cash_pct_range")
        if isinstance(rng, list) and len(rng) == 2 and all(isinstance(x, (int, float)) for x in rng):
            lo, hi = rng
            gross = sim.cash_ratio_gross
            if not lo <= gross <= hi:
                self.r.error("POSTURE_CASH_RANGE", f"以前日收盤價估算，交易後現金比 {_pct(gross)} 不在宣告區間 [{_pct(lo)}, {_pct(hi)}] 內",
                             "market_view.posture.target_cash_pct_range", SEVERE)
            elif not lo <= sim.cash_ratio <= hi:
                self.r.warn("POSTURE_CASH_RANGE_AFTER_FEES", f"扣除手續費與證交稅後，現金比 {_pct(sim.cash_ratio)} 會落在宣告區間 [{_pct(lo)}, {_pct(hi)}] 之外",
                            "market_view.posture.target_cash_pct_range")

    def _check_funding(self, book: Book, orders: list[Order], posture: dict[str, Any]) -> None:
        rng = posture.get("target_cash_pct_range")
        if not (isinstance(rng, list) and len(rng) == 2 and isinstance(rng[0], (int, float))):
            return
        lower = rng[0]
        decisions = self._decision_by_id()
        funding_ids = {did for did, d in decisions.items() if d.get("funding_for") and d.get("action") in R.SELL_ACTIONS}
        if funding_ids:
            without = [o for o in orders if o.decision_ref not in funding_ids]
            sim = simulate(book, without, self.price_of)
            if sim.cash_ratio_gross >= lower:
                self.r.error("FUNDING_NO_GAP",
                             f"拿掉資金調度賣出（{', '.join(sorted(funding_ids))}）後現金比仍有 {_pct(sim.cash_ratio_gross)}，不低於宣告下限 {_pct(lower)}；錢夠卻宣稱調度，與數字矛盾",
                             "decisions", SEVERE)
            return
        has_buy = any(o.side == "BUY" for o in orders)
        sells = [o for o in orders if o.side == "SELL"]
        if has_buy and sells:
            sim = simulate(book, [o for o in orders if o.side == "BUY"], self.price_of)
            if sim.cash_ratio_gross < lower:
                self.r.warn("FUNDING_UNTAGGED",
                            f"不賣出的話現金比會降到 {_pct(sim.cash_ratio_gross)}，低於宣告下限 {_pct(lower)}；若賣出是為了支應買進，應在賣出決策標記 funding_for",
                            "decisions")

    def _overweight_streak(self, code: str) -> int:
        dp = self.datapack or {}
        compliance = dp.get("compliance") if isinstance(dp.get("compliance"), dict) else {}
        dates: set[dt.date] = set()
        for inc in compliance.get("incidents") or []:
            if not isinstance(inc, dict) or inc.get("status") != "warn":
                continue
            msg, check = str(inc.get("message", "")), str(inc.get("check_code", "")).lower()
            if code in msg and ("weight" in check or "權重" in msg):
                try:
                    dates.add(dt.date.fromisoformat(str(inc.get("date"))))
                except ValueError:
                    pass
        try:
            day = dt.date.fromisoformat(str(dp.get("as_of_date")))
        except ValueError:
            return 0
        streak = 0
        while day in dates:
            streak += 1
            day = self._prev_trading_day(day)
        return streak

    def _check_weights(self, sim: Any) -> None:
        assert self.book
        buy_codes = {d.get("ticker") for _, d in self._decisions() if d.get("action") in R.BUY_ACTIONS}
        for code, shares in sorted(sim.shares_after.items()):
            if shares <= 0:
                continue
            w, cap = sim.weight(code), R.weight_cap(code)
            if code in buy_codes:
                if w > cap:
                    self.r.error("WEIGHT_OVER_CAP_BY_BUY", f"{self._name(code)} 加碼後權重預估 {_pct(w)}，超過上限 {_pct(cap)}；加碼造成的超限當日即記警告",
                                 "decisions", VIOLATION)
                elif w > cap - R.WEIGHT_CAP_BUFFER:
                    self.r.warn("WEIGHT_NEAR_CAP_BY_BUY", f"{self._name(code)} 加碼後權重預估 {_pct(w)}，距上限 {_pct(cap)} 很近；當日上漲可能超限", "decisions")
                continue
            if w <= cap:
                continue
            streak = self._overweight_streak(code)
            day_n = streak + 1
            if day_n > R.PASSIVE_OVERWEIGHT_GRACE_DAYS:
                self.r.error("PASSIVE_OVERWEIGHT_DEADLINE",
                             f"{self._name(code)} 權重 {_pct(w)} 超過上限 {_pct(cap)}，依資料包紀錄已連續超限 {streak} 個交易日；今天若仍超限即記警告，必須減碼到上限以下",
                             "decisions", VIOLATION)
            else:
                left = R.PASSIVE_OVERWEIGHT_GRACE_DAYS - streak
                self.r.warn("PASSIVE_OVERWEIGHT",
                            f"{self._name(code)} 權重 {_pct(w)} 超過上限 {_pct(cap)}（價格波動造成的被動超限）；依資料包紀錄已連續 {streak} 天，最晚 {left} 個交易日內要減碼到上限以下",
                            "decisions")

    def _check_active_share(self, weights: dict[str, float]) -> None:
        if self.etf_holdings is None:
            return
        try:
            listed = load_active_etf_codes(self.opts.active_etf_list_path)
        except (InputError, OSError):
            listed = {}
        missing = sorted(set(listed) - set(self.etf_holdings))
        if missing:
            self.r.warn("ACTIVE_ETF_INCOMPLETE", f"主動型 ETF 持股檔缺少 {len(missing)} 檔：{', '.join(missing)}；這些 ETF 的主動投資比率未檢查")
        worst: tuple[str, float, float] | None = None
        for etf, holdings in self.etf_holdings.items():
            norm = active_share(weights, holdings, R.ACTIVE_SHARE_TOP_N, normalize=True)
            raw = active_share(weights, holdings, R.ACTIVE_SHARE_TOP_N, normalize=False)
            label = f"{etf} {listed.get(etf, '')}".strip()
            if norm < R.ACTIVE_SHARE_MIN:
                self.r.error("ACTIVE_SHARE_TOO_LOW", f"與 {label} 前 15 大持股的主動投資比率僅 {_pct(norm)}（未正規化 {_pct(raw)}），低於 15%",
                             consequence="連續 2 個交易日即可能被取消參賽與晉級資格")
            elif norm < R.ACTIVE_SHARE_WARN or raw < R.ACTIVE_SHARE_MIN:
                self.r.warn("ACTIVE_SHARE_LOW", f"與 {label} 的主動投資比率 {_pct(norm)}（未正規化 {_pct(raw)}），接近 15% 門檻")
            if worst is None or norm < worst[1]:
                worst = (label, norm, raw)
        if worst:
            self.r.summary["最低主動投資比率"] = f"{worst[0]}：{_pct(worst[1])}"

    def _check_theme(self, weights: dict[str, float]) -> None:
        if not self.theme:
            return
        tickers = self.theme["tickers"]
        theme_weight = sum(w for c, w in weights.items() if c in tickers)
        equity_weight = sum(weights.values())
        self.r.summary[f"主題「{self.theme['name']}」占淨值"] = _pct(theme_weight)
        if theme_weight < self.theme["min_weight"]:
            self.r.warn("THEME_WEIGHT_LOW", f"主題「{self.theme['name']}」標的占淨值 {_pct(theme_weight)}，低於設定下限 {_pct(self.theme['min_weight'])}；與 ETF 投資策略說明文件的宣告不一致")
        for i, d in self._decisions():
            if d.get("action") in R.BUY_ACTIONS and d.get("ticker") not in tickers:
                self.r.info("DECISION_OFF_THEME", f"{self._name(str(d.get('ticker')))} 不在主題標的清單內；推論中應交代與策略的關聯", f"decisions[{i}]")
        if equity_weight and theme_weight / equity_weight < 0.5:
            self.r.info("THEME_MINORITY", f"主題標的只占股票部位的 {_pct(theme_weight / equity_weight)}")
