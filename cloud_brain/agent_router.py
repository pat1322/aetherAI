"""
AetherAI — Agent Router  (Stage 5)
Maps agent names to agent classes and dispatches execution.

Stage 5 change: memory_agent wired in.
All agents now receive `memory` kwarg so memory_agent can read/write preferences.
"""

import logging
from typing import Optional

from memory import MemoryManager
from utils.qwen_client import QwenClient
from utils.websocket_manager import WebSocketManager

logger = logging.getLogger(__name__)


class AgentRouter:
    """
    Receives a step (agent_name + parameters) and routes it to the correct agent.
    Agents are instantiated lazily and reused.
    """

    def __init__(self, memory: MemoryManager, ws_manager: WebSocketManager, qwen: QwenClient):
        self.memory     = memory
        self.ws_manager = ws_manager
        self.qwen       = qwen
        self._agents    = {}   # lazy cache

    def _get_agent(self, agent_name: str):
        if agent_name not in self._agents:
            self._agents[agent_name] = self._create_agent(agent_name)
        return self._agents[agent_name]

    def _create_agent(self, agent_name: str):
        # Common kwargs passed to every agent (memory added in Stage 5)
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

        else:
            logger.warning(f"Unknown agent: {agent_name}. Falling back to research_agent.")
            from agents.research_agent import ResearchAgent
            return ResearchAgent(**kw)

    async def execute_step(
        self,
        agent_name: str,
        parameters: dict,
        task_id: str,
        previous_output: str = "",
    ) -> Optional[str]:
        agent = self._get_agent(agent_name)
        return await agent.run(
            parameters=parameters,
            task_id=task_id,
            context=previous_output,
        )
