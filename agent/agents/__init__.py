"""五個 Agent。每個步驟依序嘗試 [主模型 LLM 版, 備援模型 LLM 版, 規則版]。

LLM 版在里程碑 C 實作後登記到 LLM_AGENTS；尚未登記或沒設定模型的步驟只跑規則版。
"""
from __future__ import annotations

from ..settings import Settings
from .intel import LLMIntelAgent, RuleIntelAgent
from .llm_agents import LLMMarketAgent, LLMPortfolioManagerAgent, LLMResearchAgent, LLMReviewerAgent
from .market import RuleMarketAgent
from .portfolio_manager import RulePortfolioManagerAgent
from .research import RuleResearchAgent
from .reviewer import RuleReviewerAgent

STEPS = ("intel", "market", "research", "reviewer", "portfolio_manager")
RULE_AGENTS = {
    "intel": RuleIntelAgent,
    "market": RuleMarketAgent,
    "research": RuleResearchAgent,
    "reviewer": RuleReviewerAgent,
    "portfolio_manager": RulePortfolioManagerAgent,
}
LLM_AGENTS: dict[str, type] = {
    "intel": LLMIntelAgent,
    "market": LLMMarketAgent,
    "research": LLMResearchAgent,
    "reviewer": LLMReviewerAgent,
    "portfolio_manager": LLMPortfolioManagerAgent,
}


def build_agents(settings: Settings) -> dict[str, list]:
    agents: dict[str, list] = {}
    for step in STEPS:
        chain: list = []
        cls = LLM_AGENTS.get(step)
        if cls is not None:
            for spec in (settings.llm.for_agent(step), settings.llm.fallback_for(step)):
                if spec is not None:
                    chain.append(cls(spec))
        chain.append(RULE_AGENTS[step]())
        agents[step] = chain
    return agents
