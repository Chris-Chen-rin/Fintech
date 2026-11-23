"""依模型等級限制每分鐘請求數（RPM），避免打爆 Google 免費額度。

稀缺 Flash（有 daily_limit，或名稱含 flash 但不含 lite）預設 5 次／分；
Flash-Lite／Gemma 等預設 15 次／分。以「金鑰指紋 × 等級」分開計算，備援金鑰各自有額度。
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import defaultdict, deque
from typing import Any

_LOCK = threading.Lock()
_WINDOWS: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_RPM = {"scarce": 5, "lite": 15}


def configure(scarce: int = 5, lite: int = 15) -> None:
    with _LOCK:
        _RPM["scarce"] = max(1, int(scarce))
        _RPM["lite"] = max(1, int(lite))


def key_id(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def tier_for(spec: Any) -> str:
    """稀缺：有 daily_limit，或 Flash 但非 Lite；其餘當 lite（含 Gemma）。"""
    if getattr(spec, "daily_limit", None) is not None:
        return "scarce"
    name = (getattr(spec, "model", "") or "").lower()
    if "flash" in name and "lite" not in name:
        return "scarce"
    return "lite"


def acquire(secret: str, tier: str) -> float:
    """等到這個金鑰在此等級還有 RPM 名額再放行；回傳等待秒數。"""
    kid = key_id(secret)
    limit = _RPM.get(tier, _RPM["lite"])
    waited = 0.0
    while True:
        with _LOCK:
            now = time.monotonic()
            q = _WINDOWS[(kid, tier)]
            while q and now - q[0] >= 60.0:
                q.popleft()
            if len(q) < limit:
                q.append(now)
                return waited
            sleep_for = 60.0 - (now - q[0]) + 0.05
        time.sleep(max(sleep_for, 0.05))
        waited += max(sleep_for, 0.05)


def reset() -> None:
    """測試用：清空滑動視窗。"""
    with _LOCK:
        _WINDOWS.clear()
