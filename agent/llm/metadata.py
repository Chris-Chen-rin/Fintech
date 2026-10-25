"""agent_metadata 的模型欄位：規格只能填一個 model_provider、model_version 最多 60 字。

- 全部 Agent 用同一個模型：照實填。
- 只有一家供應商但多個模型或混用規則版：填該供應商，model_version 列出各 Agent。
- 多家供應商：model_provider 填 other，model_version 列出各 Agent（例 pm:claude-x;mk:gpt-x;rs:rule）。
- 太長時改成 multi-model:<雜湊>；完整對照一律另存 archive 的 models.json，抽檢時用它對帳。
"""
from __future__ import annotations

import hashlib

ABBR = {"portfolio_manager": "pm", "market": "mk", "research": "rs", "reviewer": "rv", "intel": "in"}
ORDER = ("pm", "mk", "rs", "rv", "in")
RULE_VERSION = "rule-based-agents-v1"


def _short(model: str) -> str:
    """nvidia/nemotron-x:free → nemotron-x（完整代號在 models.json）。"""
    return model.rsplit("/", 1)[-1].removesuffix(":free")


def agent_of(step: str) -> str:
    return step.removesuffix("_retry")


def model_metadata(used: dict[str, str], models: dict[str, tuple[str, str]]) -> tuple[str, str, dict[str, str]]:
    """used：步驟 → rule/llm；models：步驟 → (D-Plan 供應商, 模型版本)。回傳 (provider, version, 完整對照)。"""
    per_agent: dict[str, tuple[str, str] | None] = {}
    for step, variant in used.items():
        ab = ABBR.get(agent_of(step), agent_of(step)[:2])
        if variant == "llm" and models.get(step):
            per_agent[ab] = models[step]
        else:
            per_agent.setdefault(ab, None)
    mapping = {ab: (f"{m[0]}:{m[1]}" if m else "rule") for ab, m in per_agent.items()}
    llm = {ab: m for ab, m in per_agent.items() if m}
    if not llm:
        return "other", RULE_VERSION, mapping
    providers = {p for p, _ in llm.values()}
    versions = {v for _, v in llm.values()}
    has_rule = len(llm) < len(per_agent)
    provider = next(iter(providers)) if len(providers) == 1 else "other"
    if len(providers) == 1 and len(versions) == 1 and not has_rule:
        return provider, next(iter(versions)), mapping
    ordered = sorted(per_agent, key=lambda ab: ORDER.index(ab) if ab in ORDER else 99)
    version = ";".join(f"{ab}:{_short(per_agent[ab][1]) if per_agent[ab] else 'rule'}" for ab in ordered)
    if len(version) > 60:
        full = ";".join(f"{ab}={mapping[ab]}" for ab in ordered)
        version = f"multi-model:{hashlib.sha256(full.encode()).hexdigest()[:12]}"
    return provider, version, mapping
