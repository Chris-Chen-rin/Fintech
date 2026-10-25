"""M11 LLM 介面：統一的「提示詞 → JSON 結果」呼叫，記錄 token 與模型實際回報的版本。"""
from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from ..settings import ModelSpec


class LLMError(Exception):
    """LLM 呼叫失敗或回傳內容無法解析。"""


@dataclass
class LLMResult:
    data: Any
    text: str
    input_tokens: int
    output_tokens: int
    model_version: str  # 供應商回報的實際模型版本（沒有就用設定值）
    seconds: float


class LLMClient(Protocol):
    spec: ModelSpec

    def complete_json(self, system: str, user: str) -> LLMResult: ...


FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json_text(text: str) -> Any:
    """接受純 JSON 或包在 ```json 區塊裡的 JSON；前後有說明文字時取第一個完整的 {...}。"""
    cleaned = FENCE.sub("", text.strip()).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError as exc:
                raise LLMError(f"模型回傳的不是合法 JSON：{exc}") from exc
        raise LLMError("模型回傳內容找不到 JSON 物件")


def post_json(url: str, headers: dict[str, str], body: dict[str, Any], timeout: float, retries: int = 3) -> dict[str, Any]:
    """POST JSON；429 與 5xx 依序等待 2、4、8 秒重試。"""
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last = ""
    for attempt in range(retries):
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json", **headers}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            last = f"HTTP {exc.code}：{detail}"
            if exc.code != 429 and exc.code < 500:
                break
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            last = str(exc)
        time.sleep(2 ** (attempt + 1))
    raise LLMError(f"呼叫 {url.split('?')[0]} 失敗：{last}")
