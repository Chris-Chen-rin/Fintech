from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from dplan_checker import rules as R

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"
EXAMPLE_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.example.yaml"
CODE_DIRS = ("agent", "dplan_checker", "config")


class SettingsError(Exception):
    """設定檔缺漏或違反比賽規則。"""


THEME_MODES = ("tickers", "industries", "rule", "llm", "none")
THEME_RULE_METRICS = ("ret_5d", "ret_20d", "ret_60d", "chg_pct", "avg_value", "volatility", "close",
                      "market_cap", "market_cap_rank", "ni_ttm", "eps_ttm", "eps_growth", "roe", "pe", "pb",
                      "rev_yoy", "rev_cum_yoy", "cash_conversion", "dividend_yield")
AGENT_NAMES = ("intel", "market", "research", "reviewer", "portfolio_manager")
LLM_PROVIDERS = ("openai", "anthropic", "google", "xai", "deepseek", "openrouter", "openai_compatible", "mock")
DPLAN_PROVIDERS = ("anthropic", "openai", "google", "xai", "deepseek", "alibaba", "meta", "mistral", "cohere",
                   "amazon", "microsoft", "nvidia", "taide", "other")


@dataclass(frozen=True)
class ThemeRule:
    """數值條件；held_* 是已持有時的退出門檻（比進入門檻寬鬆，避免成員天天進出）。"""
    metric: str
    min: float | None = None
    max: float | None = None
    held_min: float | None = None
    held_max: float | None = None


@dataclass(frozen=True)
class ThemeSettings:
    name: str
    tickers: tuple[str, ...]
    min_weight: float
    mode: str = "none"
    description: str = ""
    industries: tuple[str, ...] = ()
    rules: tuple[ThemeRule, ...] = ()
    membership_file: Path | None = None


@dataclass(frozen=True)
class RiskSettings:
    min_holdings: int
    max_holdings: int
    tsmc_max_weight: float
    stock_max_weight: float
    cash_target_range: tuple[float, float]
    first_day_cash_target: float
    no_trade_band: float
    min_weights: dict[str, float] = field(default_factory=dict)  # 個股最低持有比例；0 表示完全由分數決定

    def weight_cap(self, ticker: str) -> float:
        return self.tsmc_max_weight if ticker == R.TSMC else self.stock_max_weight

    @property
    def floors(self) -> dict[str, float]:
        return {t: w for t, w in self.min_weights.items() if w > 0}


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str
    api_key_env: str = ""
    base_url: str | None = None
    report_as: str | None = None  # D-Plan model_provider；openai_compatible（自架、其他廠商）必填
    temperature: float = 0.0
    max_output_tokens: int = 4096
    timeout: float = 120.0
    json_mode: bool = True  # 送 response_format；模型不支援時設 false，改靠提示詞要求 JSON
    extra: dict[str, Any] = field(default_factory=dict)  # 原樣併入請求，例如 seed、reasoning
    daily_limit: int | None = None  # 這個 Agent 每天最多用這個模型幾次（稀缺模型用）；用完改用下一個備援

    @property
    def dplan_provider(self) -> str:
        if self.report_as:
            return self.report_as
        if self.provider in DPLAN_PROVIDERS:
            return self.provider
        vendor = self.model.split("/", 1)[0] if "/" in self.model else ""  # OpenRouter 的模型代號是「廠商/模型」
        return vendor if vendor in DPLAN_PROVIDERS else "other"

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass(frozen=True)
class LLMSettings:
    """每個 Agent 可指定不同模型；沒指定的用 default；都沒有就只用規則版。"""
    default: ModelSpec | None = None
    agents: dict[str, ModelSpec] = field(default_factory=dict)
    fallback: dict[str, tuple[ModelSpec, ...]] = field(default_factory=dict)  # 依序嘗試的備援模型
    max_calls_per_run: int = 30  # 一次完整執行約 8～15 次（研究依產業分批）；超過就降級為規則版
    daily_call_limit: int = 150  # 所有模型合計的每日上限

    def for_agent(self, name: str) -> ModelSpec | None:
        spec = self.agents.get(name, self.default)
        return spec if spec and spec.provider != "mock" else None

    def fallbacks_for(self, name: str) -> list[ModelSpec]:
        return [s for s in self.fallback.get(name, ()) if s.provider != "mock"]

    def fallback_for(self, name: str) -> ModelSpec | None:
        chain = self.fallbacks_for(name)
        return chain[0] if chain else None

    def chain(self, name: str) -> list[ModelSpec]:
        """主模型＋備援模型（不含規則版）。"""
        return [s for s in [self.for_agent(name), *self.fallbacks_for(name)] if s]

    @property
    def enabled(self) -> bool:
        return any(self.for_agent(a) for a in AGENT_NAMES)


@dataclass(frozen=True)
class Paths:
    root: Path
    cache_root: Path | None = None  # 演練時另設 root，但沿用正式的快取

    @property
    def inbox(self) -> Path:
        return self.root / "inbox"

    @property
    def out(self) -> Path:
        return self.root / "out"

    @property
    def archive(self) -> Path:
        return self.root / "archive"

    @property
    def cache(self) -> Path:
        return self.cache_root or self.root / "cache"

    def archive_for(self, trade_date: dt.date) -> Path:
        return self.archive / trade_date.isoformat()

    def ensure(self) -> None:
        for p in (self.inbox, self.out, self.archive, self.cache):
            p.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Settings:
    team_id: str
    theme: ThemeSettings
    risk: RiskSettings
    llm: LLMSettings
    holidays: frozenset[dt.date]
    paths: Paths
    source_file: Path
    warnings: tuple[str, ...] = field(default_factory=tuple)


def load_settings(path: Path | None = None) -> Settings:
    """讀取設定：參數 > 環境變數 AGENT_SETTINGS > config/settings.yaml。"""
    path = Path(path or os.environ.get("AGENT_SETTINGS") or DEFAULT_SETTINGS_PATH)
    if not path.exists():
        raise SettingsError(f"找不到設定檔 {path}；請複製 {EXAMPLE_SETTINGS_PATH.name} 成 settings.yaml 並填入隊伍代號")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SettingsError(f"設定檔 {path.name} 不是合法的 YAML：{exc}") from exc
    if not isinstance(raw, dict):
        raise SettingsError("設定檔最外層必須是對應表（key: value）")
    return parse_settings(raw, path)


def parse_settings(raw: dict[str, Any], source: Path) -> Settings:
    errors: list[str] = []
    warnings: list[str] = []

    team_id = str(raw.get("team_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", team_id):
        errors.append(f"team_id {team_id!r} 格式不符（限 A-Z a-z 0-9 _ -，1–32 字）")
    elif team_id == "TEAM_000":
        warnings.append("team_id 仍是範本值 TEAM_000，請改成主辦方配發的代號")

    theme = _parse_theme(raw.get("theme") or {}, errors, warnings)
    risk = _parse_risk(raw.get("risk") or {}, errors)
    llm = _parse_llm(raw.get("llm") or {}, errors, warnings)

    holidays: set[dt.date] = set()
    for h in raw.get("holidays") or []:
        try:
            holidays.add(h if isinstance(h, dt.date) else dt.date.fromisoformat(str(h)))
        except ValueError:
            errors.append(f"holidays 中的 {h!r} 不是 YYYY-MM-DD 日期")

    paths_raw = raw.get("paths") or {}
    root = paths_raw.get("data_root") or os.environ.get("AGENT_DATA_ROOT") or PROJECT_ROOT
    paths = Paths(Path(root).expanduser())

    if errors:
        raise SettingsError("設定檔有誤：\n" + "\n".join(f"  - {e}" for e in errors))
    return Settings(team_id, theme, risk, llm, frozenset(holidays), paths, source, tuple(warnings))


def _num(value: Any, name: str, errors: list[str]) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        errors.append(f"{name} 必須是數字，目前是 {value!r}")
        return 0.0


def _parse_theme(raw: dict[str, Any], errors: list[str], warnings: list[str]) -> ThemeSettings:
    tickers = tuple(str(t) for t in raw.get("tickers") or [])
    industries = tuple(str(i) for i in raw.get("industries") or [])
    mode = str(raw.get("mode") or ("tickers" if tickers else "none"))
    min_weight = _num(raw.get("min_weight", 0.0), "theme.min_weight", errors)
    if mode not in THEME_MODES:
        errors.append(f"theme.mode {mode!r} 不合法，可用：{' / '.join(THEME_MODES)}")
    if not 0 <= min_weight <= 1:
        errors.append("theme.min_weight 必須介於 0 與 1 之間")

    universe = json.loads(R.DEFAULT_UNIVERSE.read_text(encoding="utf-8"))["stocks"]
    codes = {s["code"] for s in universe}
    known_industries = {s.get("industry") for s in universe}
    outside = [t for t in tickers if t not in codes]
    if outside:
        errors.append(f"theme.tickers 有不在 150 檔清單內的代號：{', '.join(outside)}")
    dup = sorted({t for t in tickers if tickers.count(t) > 1})
    if dup:
        warnings.append(f"theme.tickers 有重複代號：{', '.join(dup)}")
    unknown = [i for i in industries if i not in known_industries]
    if unknown:
        errors.append(f"theme.industries 有 150 檔中不存在的產業：{', '.join(unknown)}（產業名稱見 universe_150.json 的 industry）")

    rules: list[ThemeRule] = []
    for i, r in enumerate(raw.get("rules") or []):
        if not isinstance(r, dict) or r.get("metric") not in THEME_RULE_METRICS:
            errors.append(f"theme.rules[{i}] 的 metric 必須是 {' / '.join(THEME_RULE_METRICS)} 之一")
            continue
        vals = {k: (None if r.get(k) is None else _num(r.get(k), f"theme.rules[{i}].{k}", errors))
                for k in ("min", "max", "held_min", "held_max")}
        if vals["min"] is None and vals["max"] is None:
            errors.append(f"theme.rules[{i}] 至少要有 min 或 max")
        rules.append(ThemeRule(r["metric"], **vals))

    file_raw = raw.get("membership_file")
    membership_file = (PROJECT_ROOT / file_raw) if file_raw else None
    if mode == "tickers" and not tickers:
        warnings.append("theme.mode 為 tickers 但尚未填 theme.tickers")
    elif mode == "industries" and not industries:
        errors.append("theme.mode 為 industries 時必須填 theme.industries")
    elif mode == "rule" and not rules:
        errors.append("theme.mode 為 rule 時必須填 theme.rules")
    elif mode == "llm" and not (membership_file and membership_file.exists()):
        warnings.append("theme.mode 為 llm，但主題分類檔（theme.membership_file）不存在；本次視同沒有主題成員")
    elif mode == "none":
        warnings.append("尚未設定主題（theme.mode = none）：選股不考慮主題，主題占比檢查略過")
    return ThemeSettings(str(raw.get("name") or ""), tickers, min_weight, mode, str(raw.get("description") or ""),
                         industries, tuple(rules), membership_file)


def _model_spec(raw: Any, where: str, errors: list[str]) -> ModelSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        errors.append(f"{where} 必須是 {{provider, model, api_key_env}} 對應表")
        return None
    provider = str(raw.get("provider") or "mock")
    spec = ModelSpec(
        provider=provider, model=str(raw.get("model") or ""), api_key_env=str(raw.get("api_key_env") or ""),
        base_url=raw.get("base_url"), report_as=raw.get("report_as"),
        temperature=_num(raw.get("temperature", 0.0), f"{where}.temperature", errors),
        max_output_tokens=int(raw.get("max_output_tokens", 4096)),
        timeout=_num(raw.get("timeout", 120), f"{where}.timeout", errors),
        json_mode=bool(raw.get("json_mode", True)),
        extra=dict(raw.get("extra") or {}),
        daily_limit=int(raw["daily_limit"]) if raw.get("daily_limit") is not None else None,
    )
    if provider not in LLM_PROVIDERS:
        errors.append(f"{where}.provider {provider!r} 不支援，可用：{' / '.join(LLM_PROVIDERS)}")
    elif provider != "mock":
        if not spec.model:
            errors.append(f"{where}.provider 為 {provider} 時必須填 model")
        if not spec.api_key_env and provider != "openai_compatible":
            errors.append(f"{where} 必須填 api_key_env（金鑰所在的環境變數或 Colab Secret 名稱）")
        if provider == "openai_compatible" and not spec.base_url:
            errors.append(f"{where}.provider 為 openai_compatible 時必須填 base_url")
        if spec.report_as is not None and spec.report_as not in DPLAN_PROVIDERS:
            errors.append(f"{where}.report_as 必須是 D-Plan 允許的供應商：{' / '.join(DPLAN_PROVIDERS)}")
        if len(spec.model) > 60:
            errors.append(f"{where}.model 超過 60 字，無法填入 D-Plan 的 model_version")
    return spec


def _parse_llm(raw: dict[str, Any], errors: list[str], warnings: list[str]) -> LLMSettings:
    if "provider" in raw:  # 舊格式：llm 底下直接寫 provider / model / api_key_env
        default = _model_spec(raw, "llm", errors)
    else:
        default = _model_spec(raw.get("default"), "llm.default", errors)
    agents: dict[str, ModelSpec] = {}
    fallback: dict[str, tuple[ModelSpec, ...]] = {}
    for section in ("agents", "fallback"):
        for name, spec_raw in (raw.get(section) or {}).items():
            if name not in AGENT_NAMES:
                errors.append(f"llm.{section}.{name} 不是 Agent 名稱，可用：{' / '.join(AGENT_NAMES)}")
                continue
            if section == "agents":
                spec = _model_spec(spec_raw, f"llm.agents.{name}", errors)
                if spec:
                    agents[name] = spec
                continue
            items = spec_raw if isinstance(spec_raw, list) else [spec_raw]
            specs = [_model_spec(r, f"llm.fallback.{name}[{i}]", errors) for i, r in enumerate(items)]
            fallback[name] = tuple(s for s in specs if s)
    limits = {}
    for name in ("max_calls_per_run", "daily_call_limit"):
        if name in raw:
            value = raw[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                errors.append(f"llm.{name} 必須是非負整數")
            else:
                limits[name] = value
    llm = LLMSettings(default, agents, fallback, **limits)
    if not llm.enabled:
        warnings.append("LLM 尚未設定：五個 Agent 都使用規則版")
    return llm


def _parse_risk(raw: dict[str, Any], errors: list[str]) -> RiskSettings:
    cash = raw.get("cash_target_range", [0.03, 0.15])
    if not (isinstance(cash, list) and len(cash) == 2):
        errors.append("risk.cash_target_range 必須是 [下限, 上限]")
        cash = [0.03, 0.15]
    risk = RiskSettings(
        min_holdings=int(raw.get("min_holdings", 22)),
        max_holdings=int(raw.get("max_holdings", 28)),
        tsmc_max_weight=_num(raw.get("tsmc_max_weight", 0.23), "risk.tsmc_max_weight", errors),
        stock_max_weight=_num(raw.get("stock_max_weight", 0.09), "risk.stock_max_weight", errors),
        cash_target_range=(_num(cash[0], "risk.cash_target_range[0]", errors), _num(cash[1], "risk.cash_target_range[1]", errors)),
        first_day_cash_target=_num(raw.get("first_day_cash_target", 0.06), "risk.first_day_cash_target", errors),
        no_trade_band=_num(raw.get("no_trade_band", 0.015), "risk.no_trade_band", errors),
        min_weights={str(t): _num(w, f"risk.min_weights.{t}", errors) for t, w in (raw.get("min_weights") or {}).items()},
    )
    universe = {s["code"] for s in json.loads(R.DEFAULT_UNIVERSE.read_text(encoding="utf-8"))["stocks"]}
    for t, w in risk.min_weights.items():
        if t not in universe:
            errors.append(f"risk.min_weights 的 {t} 不在 150 檔標的範圍內")
        elif not 0 <= w <= risk.weight_cap(t):
            errors.append(f"risk.min_weights.{t} 必須介於 0 與該股權重上限 {risk.weight_cap(t):.0%}")
    if sum(risk.min_weights.values()) > 1 - risk.cash_target_range[1]:
        errors.append("risk.min_weights 合計不能超過股票部位（1 − 現金上限）")
    if not R.MIN_HOLDINGS <= risk.min_holdings <= risk.max_holdings <= R.MAX_HOLDINGS:
        errors.append(f"持股檔數須滿足 {R.MIN_HOLDINGS} ≤ min_holdings ≤ max_holdings ≤ {R.MAX_HOLDINGS}")
    if not 0 < risk.tsmc_max_weight <= R.TSMC_WEIGHT_CAP:
        errors.append(f"risk.tsmc_max_weight 必須介於 0 與 {R.TSMC_WEIGHT_CAP}")
    if not 0 < risk.stock_max_weight <= R.STOCK_WEIGHT_CAP:
        errors.append(f"risk.stock_max_weight 必須介於 0 與 {R.STOCK_WEIGHT_CAP}")
    lo, hi = risk.cash_target_range
    if not 0 <= lo <= hi <= R.CASH_MAX:
        errors.append(f"risk.cash_target_range 須滿足 0 ≤ 下限 ≤ 上限 ≤ {R.CASH_MAX}")
    elif not lo <= risk.first_day_cash_target <= hi:
        errors.append("risk.first_day_cash_target 必須落在 cash_target_range 內")
    if not 0 <= risk.no_trade_band < risk.stock_max_weight:
        errors.append("risk.no_trade_band 必須 ≥ 0 且小於個股上限")
    capacity = risk.tsmc_max_weight + (risk.max_holdings - 1) * risk.stock_max_weight
    if capacity < 1 - risk.first_day_cash_target:
        errors.append(f"在 max_holdings 與權重上限下，股票部位最多只能到 {capacity:.0%}，放不滿 1 − 首日目標現金比")
    return risk


def get_secret(name: str) -> str | None:
    """依序從環境變數、Colab Secrets、專案根目錄的 .env 讀取金鑰。"""
    if not name:
        return None
    if os.environ.get(name):
        return os.environ[name]
    try:
        from google.colab import userdata  # type: ignore[import-not-found]
        value = userdata.get(name)
        if value:
            return value
    except Exception:
        pass
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == name:
                return value.strip().strip('"').strip("'")
    return None


def code_version() -> str:
    """git commit（有未提交變更時加 -dirty）；沒有 git 時用程式檔的 sha256 前 12 碼。"""
    try:
        sha = subprocess.run(["git", "rev-parse", "--short=7", "HEAD"], cwd=PROJECT_ROOT,
                             capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", *CODE_DIRS], cwd=PROJECT_ROOT,
                               capture_output=True, text=True, check=True).stdout.strip()
        return f"git:{sha}" + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return f"sha256:{code_fingerprint()[:12]}"


def code_fingerprint() -> str:
    digest = hashlib.sha256()
    for d in CODE_DIRS:
        base = PROJECT_ROOT / d
        if not base.exists():
            continue
        for f in sorted(base.rglob("*")):
            if f.is_file() and f.suffix in {".py", ".yaml", ".yml", ".json", ".txt", ".md"} and "__pycache__" not in f.parts:
                digest.update(f.relative_to(PROJECT_ROOT).as_posix().encode())
                digest.update(f.read_bytes())
    return digest.hexdigest()
