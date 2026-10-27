"""LLM 版 Agent 的共同骨架：組提示詞 → 呼叫模型 → 解析成契約物件。

子類別只需實作 build_prompt 與 parse；每個 Agent 可設定不同模型（llm.agents.<名稱>）。
規則版永遠排在最後當備援，LLM 失敗不會讓整天交不出 D-Plan。
"""
from __future__ import annotations

import threading
from typing import Any

from ..framework.context import RunContext
from ..settings import ModelSpec


_LOCK = threading.Lock()


class LLMAgent:
    name = ""
    variant = "llm"

    def __init__(self, spec: ModelSpec, client: Any = None):
        from ..llm.providers import make_client

        self.spec = spec
        self.client = client or make_client(spec)
        self.last_model: tuple[str, str] = (spec.dplan_provider, spec.model)

    def build_prompt(self, ctx: RunContext, inputs: dict[str, Any], feedback: list[str] | None) -> tuple[str, str]:
        raise NotImplementedError

    def parse(self, ctx: RunContext, inputs: dict[str, Any], data: Any) -> Any:
        raise NotImplementedError

    def run(self, ctx: RunContext, inputs: dict[str, Any], feedback: list[str] | None = None) -> Any:
        system, user = self.build_prompt(ctx, inputs, feedback)
        from ..llm.budget import reserve

        reserve(ctx, self.name, self.spec)
        return self.parse(ctx, inputs, self.call(ctx, system, user, feedback).data)

    @staticmethod
    def with_feedback(user: str, feedback: list[str] | None) -> str:
        if not feedback:
            return user
        return user + "\n\n上一次的輸出未通過檢查，請修正：\n" + "\n".join(f"- {f}" for f in feedback[-8:])

    def call(self, ctx: RunContext, system: str, user: str, feedback: list[str] | None = None, tag: str = "") -> Any:
        """呼叫一次模型並記錄（額度須先由呼叫端 reserve）；可在多執行緒中並行。"""
        from ..llm.budget import charge

        user = self.with_feedback(user, feedback)
        name = self.name + (f"_{tag}" if tag else "")
        if hasattr(self.client, "on_retry"):
            self.client.on_retry = lambda: charge(ctx, self.name, self.spec)
        try:
            result = self.client.complete_json(system, user)
        except Exception as exc:
            if getattr(exc, "raw", None):
                ctx.save(f"llm/{name}_failed_{ctx.llm_calls:02d}.json",
                         {"model": self.spec.label, "error": str(exc), "system": system, "user": user, "response": exc.raw})
            raise
        with _LOCK:
            ctx.usage["input_tokens"] += result.input_tokens
            ctx.usage["output_tokens"] += result.output_tokens
            ctx.usage["tool_use_count"] += 1
            seq = ctx.usage["tool_use_count"]
        self.last_model = (self.spec.dplan_provider, result.model_version[:60])
        ctx.save(f"llm/{name}_{seq:02d}.json",
                 {"model": self.spec.label, "reported_model": result.model_version, "system": system, "user": user,
                  "response": result.text, "input_tokens": result.input_tokens,
                  "output_tokens": result.output_tokens, "seconds": round(result.seconds, 2)})
        return result
