"""HTTP 抓取：原始回應存證（gzip）、同一網址不重抓、證交所網站限速。"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import http.client
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..trading_calendar import iso_tpe, now_tpe

USER_AGENT = "Mozilla/5.0 (compatible; esun-agent-fund-manager/1.0)"
MIN_INTERVAL = {"www.twse.com.tw": 3.0, "www.tpex.org.tw": 1.0, "mopsov.twse.com.tw": 3.0, "openapi.twse.com.tw": 1.0}


class FetchError(Exception):
    """資料源無法取得或內容不可用。"""


@dataclass(frozen=True)
class Fetched:
    url: str
    data: Any
    fetched_at: str
    snapshot: Path
    from_cache: bool


class HttpClient:
    def __init__(self, cache_dir: Path, offline: bool = False, timeout: float = 30.0):
        self.raw_dir = cache_dir / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.offline = offline
        self.timeout = timeout
        self._last_hit: dict[str, float] = {}
        self.request_count = 0

    def _paths(self, url: str, tag: str = "") -> tuple[Path, Path]:
        key = hashlib.sha256((url + ("#" + tag if tag else "")).encode()).hexdigest()[:24]
        return self.raw_dir / f"{key}.body.gz", self.raw_dir / f"{key}.meta.json"

    def get_json(self, url: str, refresh: bool = False, tag: str = "") -> Fetched:
        """取得 JSON。快取存在且 refresh=False 時直接用快取（歷史資料不會變）。

        tag：只提供「最新一期」的資料集用日期當 tag，每天各存一份，重跑同一天時讀到同一份內容。
        """
        f = self._get(url, refresh, tag, "application/json")
        try:
            return Fetched(url, json.loads(f.data.decode("utf-8-sig")), f.fetched_at, f.snapshot, f.from_cache)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.forget(url, tag)
            raise FetchError(f"{url} 回傳的不是 JSON（可能被限速或網站維護）") from exc

    def get_text(self, url: str, refresh: bool = False, tag: str = "", encoding: str = "utf-8") -> Fetched:
        f = self._get(url, refresh, tag, "text/html")
        return Fetched(url, f.data.decode(encoding, "replace"), f.fetched_at, f.snapshot, f.from_cache)

    def _get(self, url: str, refresh: bool, tag: str, accept: str) -> Fetched:
        body_path, meta_path = self._paths(url, tag)
        if body_path.exists() and meta_path.exists() and not refresh:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return Fetched(url, gzip.decompress(body_path.read_bytes()), meta["fetched_at"], body_path, True)
        if self.offline:
            raise FetchError(f"離線模式且沒有快取：{url}")

        host = urlparse(url).hostname or ""
        wait = MIN_INTERVAL.get(host, 0.0) - (time.monotonic() - self._last_hit.get(host, -1e9))
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
        body = b""
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read()
                break
            except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
                if attempt == 2:
                    raise FetchError(f"無法連線 {url}：{exc}") from exc
                time.sleep(2 * (attempt + 1))
            finally:
                self._last_hit[host] = time.monotonic()
        self.request_count += 1
        fetched_at = iso_tpe(now_tpe())
        body_path.write_bytes(gzip.compress(body))
        meta_path.write_text(json.dumps({"url": url, "tag": tag, "fetched_at": fetched_at, "bytes": len(body)},
                                        ensure_ascii=False), encoding="utf-8")
        return Fetched(url, body, fetched_at, body_path, False)

    def forget(self, url: str, tag: str = "") -> None:
        for p in self._paths(url, tag):
            p.unlink(missing_ok=True)


def roc_date(day: dt.date) -> str:
    """民國日期 1151022。"""
    return f"{day.year - 1911}{day:%m%d}"
