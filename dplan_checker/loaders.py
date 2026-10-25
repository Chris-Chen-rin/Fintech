from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class InputError(Exception):
    """輸入檔無法讀取或格式不符。"""


@dataclass
class LoadedJson:
    data: Any
    has_bom: bool = False
    duplicate_keys: list[str] = field(default_factory=list)
    size_bytes: int = 0


def load_json(path: Path) -> LoadedJson:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise InputError(f"無法讀取檔案 {path}：{exc}") from exc

    has_bom = raw.startswith(b"\xef\xbb\xbf")
    if has_bom:
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InputError(f"{path.name} 不是 UTF-8 編碼：{exc}") from exc

    duplicates: list[str] = []

    def collect_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        for key, value in pairs:
            if key in obj:
                duplicates.append(key)
            obj[key] = value
        return obj

    def reject_constant(name: str) -> Any:
        raise ValueError(f"含非標準 JSON 常數 {name}")

    try:
        data = json.loads(text, object_pairs_hook=collect_pairs, parse_constant=reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise InputError(f"{path.name} 不是合法 JSON：{exc}") from exc
    return LoadedJson(data, has_bom, duplicates, len(raw))


def load_prices(path: Path) -> dict[str, float]:
    """讀取前日收盤價。支援 {"2330": 1480}、{"prices": {...}}、[{"code":..,"close":..}] 與 CSV。"""
    if path.suffix.lower() == ".csv":
        return _load_prices_csv(path)
    data = load_json(path).data
    if isinstance(data, dict) and isinstance(data.get("prices"), (dict, list)):
        data = data["prices"]
    if isinstance(data, dict):
        return {str(k): _to_price(v, k) for k, v in data.items()}
    if isinstance(data, list):
        out: dict[str, float] = {}
        for row in data:
            if not isinstance(row, dict):
                raise InputError("價格清單中的每一列都必須是物件")
            code = row.get("code") or row.get("ticker")
            price = row.get("close", row.get("close_price", row.get("price")))
            if code is None:
                raise InputError(f"價格列缺少 code/ticker：{row}")
            out[str(code)] = _to_price(price, code)
        return out
    raise InputError("無法辨識價格檔格式")


def _load_prices_csv(path: Path) -> dict[str, float]:
    out: dict[str, float] = {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = {f.lower(): f for f in reader.fieldnames or []}
        code_col = fields.get("code") or fields.get("ticker")
        price_col = fields.get("close") or fields.get("close_price") or fields.get("price")
        if not code_col or not price_col:
            raise InputError("CSV 需要 code（或 ticker）與 close（或 close_price / price）欄位")
        for row in reader:
            out[row[code_col].strip()] = _to_price(row[price_col], row[code_col])
    return out


def _to_price(value: Any, code: Any) -> float:
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise InputError(f"{code} 的價格不是數字：{value!r}") from exc
    if not price > 0:
        raise InputError(f"{code} 的價格必須大於 0：{value!r}")
    return price


def load_universe(path: Path) -> dict[str, str]:
    data = load_json(path).data
    stocks = data.get("stocks") if isinstance(data, dict) else data
    if not isinstance(stocks, list):
        raise InputError("標的池檔需包含 stocks 陣列")
    return {str(s["code"]): str(s.get("name", "")) for s in stocks}


def load_active_etf_codes(path: Path) -> dict[str, str]:
    data = load_json(path).data
    return {str(e["code"]): str(e.get("name", "")) for e in data.get("etfs", [])}


def load_active_etf_holdings(path: Path) -> dict[str, dict[str, float]]:
    """主動型 ETF 前 15 大持股權重。格式：{"00980A": {"2330": 0.09, ...}} 或 {"etfs": {"00980A": {"top15": {...}}}}。"""
    data = load_json(path).data
    if isinstance(data, dict) and isinstance(data.get("etfs"), dict):
        data = data["etfs"]
    if not isinstance(data, dict):
        raise InputError("主動型 ETF 持股檔格式需為物件")
    out: dict[str, dict[str, float]] = {}
    for etf, body in data.items():
        holdings = body.get("top15", body.get("holdings", body)) if isinstance(body, dict) else None
        if not isinstance(holdings, dict):
            raise InputError(f"{etf} 的持股格式需為 {{代號: 權重}}")
        out[str(etf)] = {str(k): float(v) for k, v in holdings.items() if isinstance(v, (int, float))}
    return out


def load_theme(path: Path) -> dict[str, Any]:
    """ETF 主題設定：{"name": "...", "tickers": [...], "min_weight": 0.6}。"""
    data = load_json(path).data
    if not isinstance(data, dict) or not isinstance(data.get("tickers"), list):
        raise InputError("主題檔需包含 tickers 陣列")
    return {
        "name": str(data.get("name", "主題")),
        "tickers": {str(t) for t in data["tickers"]},
        "min_weight": float(data.get("min_weight", 0.0)),
    }
