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
    """接受純 JSON 或包在 ```json 區塊裡的 JSON；前後有說明文字時取第一個完整的 {...}。
    仍解析不了時嘗試修復常見錯誤（字串內未跳脫的引號、多餘逗號、輸出被截斷）。"""
    cleaned = FENCE.sub("", text.strip()).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0:
        raise LLMError("模型回傳內容找不到 JSON 物件")
    if end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(repair_json(cleaned[start:]))
    except json.JSONDecodeError as exc:
        raise LLMError(f"模型回傳的不是合法 JSON（修復後仍失敗）：{exc}") from exc


_CLOSERS = {"{": "}", "[": "]"}


def repair_json(text: str) -> str:
    """逐字掃描修復：字串內的引號若後面不是 , } ] : 就視為內文並跳脫；刪掉 } ] 前多餘的逗號；
    結尾不完整時退回到最後一個完整元素，再補上缺的括號。"""
    out: list[str] = []
    stack: list[str] = []
    in_str = escaped = False
    cut: tuple[int, list[str]] | None = None  # (out 長度, 當時的 stack)：最後一個完整值結束處
    cut_item: tuple[int, list[str]] | None = None  # 陣列中最後一個完整元素結束處
    for i, ch in enumerate(text):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                rest = text[i + 1:].lstrip()
                if rest and rest[0] not in ",}]:":
                    out.append('\\"')
                    continue
                in_str = False
            elif ch == "\n":
                out.append("\\n")
                continue
            out.append(ch)
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            while out and out[-1] in " \n\r\t":
                out.pop()
            if out and out[-1] == ",":
                out.pop()
            if not stack:
                break
            stack.pop()
            out.append(ch)
            if not stack:
                return "".join(out)
            cut = (len(out), list(stack))
            if stack[-1] == "[":
                cut_item = cut
            continue
        elif ch == "," and stack:
            cut = (len(out), list(stack))
            if stack[-1] == "[":
                cut_item = cut
        out.append(ch)
    if cut is None:
        return "".join(out) + ('"' if in_str else "") + "".join(_CLOSERS[c] for c in reversed(stack))
    if cut_item and stack[:len(cut_item[1])] == cut_item[1] and len(stack) > len(cut_item[1]):
        cut = cut_item  # 截斷在陣列的某個元素裡：丟掉不完整的元素
    length, open_ = cut
    body = "".join(out[:length]).rstrip().rstrip(",")
    return body + "".join(_CLOSERS[c] for c in reversed(open_))


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
