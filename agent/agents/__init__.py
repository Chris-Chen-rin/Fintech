"""五個 Agent。每個步驟依序嘗試 [LLM 版, 規則版]；LLM 版在里程碑 C 接上。"""
from __future__ import annotations

from ..settings import Settings
from .intel import RuleIntelAgent
from .market import RuleMarketAgent
from .portfolio_manager import RulePortfolioManagerAgent
from .research import RuleResearchAgent
from .reviewer import RuleReviewerAgent

STEPS = ("intel", "market", "research", "reviewer", "portfolio_manager")


def build_agents(settings: Settings) -> dict[str, list]:
    return {
        "intel": [RuleIntelAgent()],
        "market": [RuleMarketAgent()],
        "research": [RuleResearchAgent()],
        "reviewer": [RuleReviewerAgent()],
        "portfolio_manager": [RulePortfolioManagerAgent()],
    }
