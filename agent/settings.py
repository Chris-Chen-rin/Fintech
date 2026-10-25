from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from dplan_checker import rules as R
from dplan_checker.loaders import load_universe

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"
EXAMPLE_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.example.yaml"
CODE_DIRS = ("agent", "dplan_checker", "config")


class SettingsError(Exception):
    """設定檔缺漏或違反比賽規則。"""


@dataclass(frozen=True)
class ThemeSettings:
    name: str
    tickers: tuple[str, ...]
    min_weight: float


@dataclass(frozen=True)
class RiskSettings:
    min_holdings: int
    max_holdings: int
    tsmc_max_weight: float
    stock_max_weight: float
    cash_target_range: tuple[float, float]
    first_day_cash_target: float
    no_trade_band: float

    def weight_cap(self, ticker: str) -> float:
        return self.tsmc_max_weight if ticker == R.TSMC else self.stock_max_weight


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    model: str
    api_key_env: str


@dataclass(frozen=True)
class Paths:
    root: Path

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
        return self.root / "cache"

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

    theme_raw = raw.get("theme") or {}
    tickers = tuple(str(t) for t in theme_raw.get("tickers") or [])
    theme = ThemeSettings(str(theme_raw.get("name") or ""), tickers, _num(theme_raw.get("min_weight", 0.0), "theme.min_weight", errors))
    universe = load_universe(R.DEFAULT_UNIVERSE)
    outside = [t for t in tickers if t not in universe]
    if outside:
        errors.append(f"theme.tickers 有不在 150 檔清單內的代號：{', '.join(outside)}")
    dup = sorted({t for t in tickers if tickers.count(t) > 1})
    if dup:
        warnings.append(f"theme.tickers 有重複代號：{', '.join(dup)}")
    if not tickers:
        warnings.append("尚未設定主題標的（theme.tickers）；首日建倉前必須填好")
    if not 0 <= theme.min_weight <= 1:
        errors.append("theme.min_weight 必須介於 0 與 1 之間")

    risk = _parse_risk(raw.get("risk") or {}, errors)

    llm_raw = raw.get("llm") or {}
    llm = LLMSettings(str(llm_raw.get("provider") or "mock"), str(llm_raw.get("model") or ""), str(llm_raw.get("api_key_env") or ""))
    if llm.provider == "mock":
        warnings.append("LLM 尚未設定（provider=mock），推論文字會使用模板")
    elif not llm.model:
        errors.append(f"llm.provider 為 {llm.provider} 時必須填 llm.model")

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
    )
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
