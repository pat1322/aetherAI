"""
AetherAI — Agent Router  (Stage 6 — streaming patch)

Streaming addition
──────────────────
execute_step() now calls agent.set_stream_context(task_id) before
agent.run() so every agent knows which task to stream chunks to.
The "chat" pseudo-agent is unchanged — it streams via the orchestrator.
"""

import logging
from typing import Optional

from memory import MemoryManager
from utils.qwen_client import QwenClient
from utils.websocket_manager import WebSocketManager

logger = logging.getLogger(__name__)


class AgentRouter:
    def __init__(self, memory: MemoryManager, ws_manager: WebSocketManager,
                 qwen: QwenClient):
        self.memory     = memory
        self.ws_manager = ws_manager
        self.qwen       = qwen
        self._agents    = {}

    def _get_agent(self, agent_name: str):
        if agent_name not in self._agents:
            self._agents[agent_name] = self._create_agent(agent_name)
        return self._agents[agent_name]

    def _create_agent(self, agent_name: str):
        kw = dict(qwen=self.qwen, ws_manager=self.ws_manager, memory=self.memory)

        if agent_name == "research_agent":
            from agents.research_agent import ResearchAgent
            return ResearchAgent(**kw)
        elif agent_name == "document_agent":
            from agents.document_agent import DocumentAgent
            return DocumentAgent(**kw)
        elif agent_name == "browser_agent":
            from agents.browser_agent import BrowserAgent
            return BrowserAgent(**kw)
        elif agent_name == "coding_agent":
            from agents.coding_agent import CodingAgent
            return CodingAgent(**kw)
        elif agent_name == "automation_agent":
            from agents.automation_agent import AutomationAgent
            return AutomationAgent(**kw)
        elif agent_name == "memory_agent":
            from agents.memory_agent import MemoryAgent
            return MemoryAgent(**kw)
        elif agent_name == "weather_agent":
            from agents.weather_agent import WeatherAgent
            return WeatherAgent(**kw)
        elif agent_name == "crypto_agent":
            from agents.crypto_agent import CryptoAgent
            return CryptoAgent(**kw)
        elif agent_name == "news_agent":
            from agents.news_agent import NewsAgent
            return NewsAgent(**kw)
        elif agent_name == "finance_agent":
            from agents.finance_agent import FinanceAgent
            return FinanceAgent(**kw)
        else:
            logger.warning(f"Unknown agent: {agent_name}. Falling back to research_agent.")
            from agents.research_agent import ResearchAgent
            return ResearchAgent(**kw)

    async def execute_step(self, agent_name: str, parameters: dict,
                           task_id: str, previous_output: str = "",
                           session_id: str = "") -> Optional[str]:
        # "chat" pseudo-agent — handled directly via qwen.answer()
        # Streaming for chat is handled in orchestrator, not here
        if agent_name == "chat":
            query = parameters.get("query", "") or previous_output or ""
            return await self.qwen.answer(question=query)

        agent = self._get_agent(agent_name)

        # Inject stream context — task_id AND session_id so streaming
        # chunks only go to the session that created this task.
        agent.set_stream_context(task_id, session_id)

        return await agent.run(
            parameters=parameters,
            task_id=task_id,
            context=previous_output,
        )
