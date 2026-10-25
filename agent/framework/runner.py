"""執行單一 Agent：驗證契約、重試、降級為規則版、存下輸入輸出。"""
from __future__ import annotations

import time
import traceback
from typing import Any, Callable, Protocol

from .context import RunContext


class AgentFailure(Exception):
    """所有版本的 Agent 都失敗。"""


class Agent(Protocol):
    name: str
    variant: str  # rule / llm

    def run(self, ctx: RunContext, inputs: dict[str, Any], feedback: list[str] | None = None) -> Any: ...


class AgentRunner:
    def __init__(self, ctx: RunContext):
        self.ctx = ctx
        self.seq = 0
        self.used: dict[str, str] = {}
        self.models: dict[str, tuple[str, str]] = {}  # 步驟 → (D-Plan 供應商, 模型版本)

    def run(self, step: str, agents: list[Agent], inputs: dict[str, Any],
            validate: Callable[[str, Any], None], feedback: list[str] | None = None,
            attempts: int = 2) -> Any:
        """依序嘗試 agents（通常是 [LLM 版, 規則版]）。LLM 版最多 attempts 次，失敗原因當作 feedback 傳給下一次；
        規則版只跑一次，且只看原始 feedback（LLM 的失敗訊息與它無關）。"""
        notes = list(feedback or [])
        for agent in agents:
            if agent.variant != "llm":
                notes = list(feedback or [])
            for attempt in range(1, (attempts if agent.variant == "llm" else 1) + 1):
                self.seq += 1
                started = time.monotonic()
                output: Any = None
                record: dict[str, Any] = {"step": step, "agent": agent.name, "variant": agent.variant,
                                          "attempt": attempt, "feedback": notes, "inputs": inputs}
                try:
                    output = agent.run(self.ctx, inputs, notes or None)
                    validate(f"{agent.name}（{agent.variant}）", output)
                except Exception as exc:  # noqa: BLE001 - 任何失敗都要降級
                    record["error"] = str(exc)
                    record["traceback"] = traceback.format_exc()
                    problems = getattr(exc, "problems", None) or [str(exc)]
                    notes = notes + [f"[{agent.name}/{agent.variant} 第 {attempt} 次] " + p for p in problems]
                    self.ctx.log(f"  ✗ {step}：{agent.name}（{agent.variant}）失敗：{exc}")
                    if getattr(exc, "final", False):
                        break
                    continue
                finally:
                    record["seconds"] = round(time.monotonic() - started, 3)
                    self.ctx.save(f"agents/{self.seq:02d}_{step}_{agent.variant}.json", record | {"output": output})
                self.used[step] = agent.variant
                if agent.variant == "llm":
                    self.models[step] = agent.last_model
                return output
        raise AgentFailure(f"{step} 的所有 Agent 版本都失敗：" + "；".join(notes[-3:]))
