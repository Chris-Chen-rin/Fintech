"""各供應商的轉接器。只用標準函式庫呼叫 REST API，不需額外套件。"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from ..settings import ModelSpec, get_secret
from .base import LLMError, LLMResult, Unavailable, parse_json_text, post_json
from .rate_limit import acquire, tier_for

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


def _key_chain(spec: ModelSpec, backup_env: str = "") -> list[tuple[str, str]]:
    """主金鑰＋備援金鑰（有找到的才列入）。"""
    envs = [spec.api_key_env]
    if backup_env and backup_env not in envs:
        envs.append(backup_env)
    out = [(e, get_secret(e) or "") for e in envs if e and get_secret(e)]
    if not out and spec.api_key_env:
        raise LLMError(f"找不到金鑰 {spec.api_key_env}" + (f"／{backup_env}" if backup_env else ""))
    return out


def _looks_like_quota(text: str) -> bool:
    t = text.lower()
    return any(s in t for s in ("quota", "resource_exhausted", "resource exhausted", "billing"))


class _Client:
    """遇到 429／5xx 最多送 3 次（間隔 4、8 秒）；送出前依 RPM 排隊。
    Google 免費額度約：稀缺 Flash 20 次／天／模型、Flash-Lite 500 次／天／模型。"""
    on_retry: Callable[[], None] | None = None
    backup_env: str = ""
    _key_i: int = 0

    def _gated_post(self, url: str, headers: dict[str, str], body: dict[str, Any], secret: str) -> dict[str, Any]:
        tier = tier_for(self.spec)

        def on_retry() -> None:
            acquire(secret, tier)
            if self.on_retry:
                self.on_retry()

        acquire(secret, tier)
        return post_json(url, headers, body, self.spec.timeout, retries=3, on_retry=on_retry)

    def _post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        secret = headers.get("x-goog-api-key") or ""
        if secret.startswith("Bearer "):
            secret = secret[7:]
        if not secret and "Authorization" in headers:
            secret = headers["Authorization"].removeprefix("Bearer ").strip()
        return self._gated_post(url, headers, body, secret or "anon")


class OpenAICompatibleClient(_Client):
    """OpenAI、xAI、DeepSeek、OpenRouter 與任何相容 OpenAI Chat Completions 的服務（自架模型用 base_url）。"""

    def __init__(self, spec: ModelSpec, backup_env: str = ""):
        self.spec = spec
        self.backup_env = backup_env
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
        data = self._post(f"{self.base}/chat/completions", headers, body)
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


class AnthropicClient(_Client):
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, spec: ModelSpec, backup_env: str = ""):
        self.spec = spec
        self.backup_env = backup_env

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
        data = self._post(self.spec.base_url or self.URL, headers, body)
        text = "".join(part.get("text", "") for part in data.get("content") or [] if part.get("type") == "text")
        if not text:
            raise LLMError(f"回應沒有文字內容：{str(data)[:300]}")
        usage = data.get("usage") or {}
        return LLMResult(parse_json_text(text), text, int(usage.get("input_tokens") or 0),
                         int(usage.get("output_tokens") or 0), str(data.get("model") or self.spec.model),
                         time.monotonic() - started)


class GeminiClient(_Client):
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, spec: ModelSpec, backup_env: str = ""):
        self.spec = spec
        self.backup_env = backup_env
        self._key_i = 0

    def complete_json(self, system: str, user: str) -> LLMResult:
        url = (self.spec.base_url or self.URL).format(model=self.spec.model)
        body = {
            "systemInstruction": {"parts": [{"text": system + JSON_INSTRUCTION}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": self.spec.temperature, "maxOutputTokens": self.spec.max_output_tokens,
                                 "responseMimeType": "application/json", **self.spec.extra},
        }
        keys = _key_chain(self.spec, self.backup_env)
        started = time.monotonic()
        last: Exception | None = None
        n = len(keys)
        start = self._key_i % n
        for offset in range(n):
            i = (start + offset) % n
            _env, key = keys[i]
            try:
                data = self._gated_post(url, {"x-goog-api-key": key}, body, key)
                self._key_i = i
                break
            except Unavailable as exc:
                last = exc
                if offset + 1 < n and _looks_like_quota(str(exc)):
                    continue
                raise
        else:
            raise last or LLMError("沒有可用的 Gemini 金鑰")
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


def make_client(spec: ModelSpec, backup_env: str = ""):
    if spec.provider in ("openai", "xai", "deepseek", "openrouter", "openai_compatible"):
        return OpenAICompatibleClient(spec, backup_env)
    if spec.provider == "anthropic":
        return AnthropicClient(spec, backup_env)
    if spec.provider == "google":
        return GeminiClient(spec, backup_env)
    raise LLMError(f"不支援的供應商 {spec.provider}")
