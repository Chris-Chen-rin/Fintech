"""A1 情報 Agent：從新聞與重大訊息擷取文字型事實（L1）。

規則版不產生文字型事實：沒有 LLM 就無法可靠地從原文擷取事實，只用量化工具的數字型事實。
LLM 版（里程碑 C）會讀原文並輸出 Fact，程式再驗證數字出現在原文中。
"""
from __future__ import annotations

from typing import Any

from ..framework.context import RunContext
from ..framework.contracts import Fact


class RuleIntelAgent:
    name = "intel"
    variant = "rule"

    def run(self, ctx: RunContext, inputs: dict[str, Any], feedback: list[str] | None = None) -> list[Fact]:
        return []
