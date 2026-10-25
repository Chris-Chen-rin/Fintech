"""LLM 版 Agent 的共同骨架：組提示詞 → 呼叫模型 → 解析成契約物件。

子類別只需實作 build_prompt 與 parse；每個 Agent 可設定不同模型（llm.agents.<名稱>）。
規則版永遠排在最後當備援，LLM 失敗不會讓整天交不出 D-Plan。
"""
from __future__ import annotations

from typing import Any

from ..framework.context import RunContext
from ..settings import ModelSpec


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
        if feedback:
            user += "\n\n上一次的輸出未通過檢查，請修正：\n" + "\n".join(f"- {f}" for f in feedback[-8:])
        result = self.client.complete_json(system, user)
        ctx.usage["input_tokens"] += result.input_tokens
        ctx.usage["output_tokens"] += result.output_tokens
        ctx.usage["tool_use_count"] += 1
        self.last_model = (self.spec.dplan_provider, result.model_version[:60])
        ctx.save(f"llm/{self.name}_{ctx.usage['tool_use_count']:02d}.json",
                 {"model": self.spec.label, "reported_model": result.model_version, "system": system, "user": user,
                  "response": result.text, "input_tokens": result.input_tokens,
                  "output_tokens": result.output_tokens, "seconds": round(result.seconds, 2)})
        return self.parse(ctx, inputs, result.data)
