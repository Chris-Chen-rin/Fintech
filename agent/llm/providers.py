"""各供應商的轉接器。只用標準函式庫呼叫 REST API，不需額外套件。"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from ..settings import ModelSpec, get_secret
from .base import LLMError, LLMResult, parse_json_text, post_json

OPENAI_COMPATIBLE_BASE = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "xai": "https://api.x.ai/v1",
    "deepseek": "https://api.deepseek.com/v1",
}
JSON_INSTRUCTION = "\n\n只輸出一個 JSON 物件，不要加任何說明文字或 Markdown。"


def _key(spec: ModelSpec, required: bool = True) -> str:
    key = get_secret(spec.api_key_env) if spec.api_key_env else None
    if required and not key:
        raise LLMError(f"找不到金鑰 {spec.api_key_env}（環境變數、Colab Secrets 或 .env）")
    return key or ""


class OpenAICompatibleClient:
    """OpenAI、xAI、DeepSeek、OpenRouter 與任何相容 OpenAI Chat Completions 的服務（自架模型用 base_url）。"""

    def __init__(self, spec: ModelSpec):
        self.spec = spec
        self.base = (spec.base_url or OPENAI_COMPATIBLE_BASE.get(spec.provider, "")).rstrip("/")

    def complete_json(self, system: str, user: str) -> LLMResult:
        key = _key(self.spec, required=self.spec.provider != "openai_compatible")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        body = {
            "model": self.spec.model,
            "messages": [{"role": "system", "content": system + JSON_INSTRUCTION}, {"role": "user", "content": user}],
            "temperature": self.spec.temperature,
            "max_tokens": self.spec.max_output_tokens,
            **self.spec.extra,
        }
        if self.spec.json_mode:
            body["response_format"] = {"type": "json_object"}
        started = time.monotonic()
        data = post_json(f"{self.base}/chat/completions", headers, body, self.spec.timeout)
        if data.get("error"):
            raise LLMError(f"供應商回報錯誤：{str(data['error'])[:300]}")
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"回應格式不符：{str(data)[:300]}") from exc
        usage = data.get("usage") or {}
        try:
            parsed = parse_json_text(text)
        except LLMError as exc:
            exc.raw = text or str(data)[:2000]
            raise
        return LLMResult(parsed, text, int(usage.get("prompt_tokens") or 0),
                         int(usage.get("completion_tokens") or 0), str(data.get("model") or self.spec.model),
                         time.monotonic() - started)


class AnthropicClient:
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, spec: ModelSpec):
        self.spec = spec

    def complete_json(self, system: str, user: str) -> LLMResult:
        headers = {"x-api-key": _key(self.spec), "anthropic-version": "2023-06-01"}
        body = {
            "model": self.spec.model,
            "max_tokens": self.spec.max_output_tokens,
            "temperature": self.spec.temperature,
            "system": system + JSON_INSTRUCTION,
            "messages": [{"role": "user", "content": user}],
        }
        started = time.monotonic()
        data = post_json(self.spec.base_url or self.URL, headers, body, self.spec.timeout)
        text = "".join(part.get("text", "") for part in data.get("content") or [] if part.get("type") == "text")
        if not text:
            raise LLMError(f"回應沒有文字內容：{str(data)[:300]}")
        usage = data.get("usage") or {}
        return LLMResult(parse_json_text(text), text, int(usage.get("input_tokens") or 0),
                         int(usage.get("output_tokens") or 0), str(data.get("model") or self.spec.model),
                         time.monotonic() - started)


class GeminiClient:
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, spec: ModelSpec):
        self.spec = spec

    def complete_json(self, system: str, user: str) -> LLMResult:
        url = (self.spec.base_url or self.URL).format(model=self.spec.model)
        body = {
            "systemInstruction": {"parts": [{"text": system + JSON_INSTRUCTION}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": self.spec.temperature, "maxOutputTokens": self.spec.max_output_tokens,
                                 "responseMimeType": "application/json", **self.spec.extra},
        }
        started = time.monotonic()
        data = post_json(url, {"x-goog-api-key": _key(self.spec)}, body, self.spec.timeout)
        try:
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"] if not p.get("thought"))
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"回應格式不符（可能被安全機制擋下）：{str(data)[:300]}") from exc
        usage = data.get("usageMetadata") or {}
        try:
            parsed = parse_json_text(text)
        except LLMError as exc:
            exc.raw = text or str(data)[:2000]
            raise
        return LLMResult(parsed, text, int(usage.get("promptTokenCount") or 0),
                         int(usage.get("candidatesTokenCount") or 0), str(data.get("modelVersion") or self.spec.model),
                         time.monotonic() - started)


class MockClient:
    """測試用：responder(system, user) 回傳 dict 或 JSON 字串。"""

    def __init__(self, spec: ModelSpec, responder: Callable[[str, str], Any]):
        self.spec = spec
        self.responder = responder
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system: str, user: str) -> LLMResult:
        self.calls.append((system, user))
        out = self.responder(system, user)
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        return LLMResult(parse_json_text(text), text, len(system + user) // 2, len(text) // 2, self.spec.model, 0.0)


def make_client(spec: ModelSpec):
    if spec.provider in ("openai", "xai", "deepseek", "openrouter", "openai_compatible"):
        return OpenAICompatibleClient(spec)
    if spec.provider == "anthropic":
        return AnthropicClient(spec)
    if spec.provider == "google":
        return GeminiClient(spec)
    raise LLMError(f"不支援的供應商 {spec.provider}")
