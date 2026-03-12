"""
AetherAI — Agent Router
Maps agent names to agent classes and dispatches execution.
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
        self.memory = memory
        self.ws_manager = ws_manager
        self.qwen = qwen
        self._agents = {}   # lazy cache

    def _get_agent(self, agent_name: str):
        """Lazily initialize and cache agents."""
        if agent_name not in self._agents:
            agent = self._create_agent(agent_name)
            self._agents[agent_name] = agent
        return self._agents[agent_name]

    def _create_agent(self, agent_name: str):
        """Instantiate the correct agent class."""
        # Import here to avoid circular imports and allow selective loading
        if agent_name == "research_agent":
            from agents.research_agent import ResearchAgent
            return ResearchAgent(qwen=self.qwen)

        elif agent_name == "document_agent":
            from agents.document_agent import DocumentAgent
            return DocumentAgent(qwen=self.qwen)

        elif agent_name == "browser_agent":
            from agents.browser_agent import BrowserAgent
            return BrowserAgent(qwen=self.qwen)

        elif agent_name == "coding_agent":
            from agents.coding_agent import CodingAgent
            return CodingAgent(qwen=self.qwen)

        elif agent_name == "automation_agent":
            from agents.automation_agent import AutomationAgent
            return AutomationAgent(qwen=self.qwen, ws_manager=self.ws_manager)

        else:
            logger.warning(f"Unknown agent: {agent_name}. Falling back to research_agent.")
            from agents.research_agent import ResearchAgent
            return ResearchAgent(qwen=self.qwen)

    async def execute_step(
        self,
        agent_name: str,
        parameters: dict,
        task_id: str,
        previous_output: str = "",
    ) -> Optional[str]:
        """
        Route a step to the correct agent and return its output.
        """
        agent = self._get_agent(agent_name)

        return await agent.run(
            parameters=parameters,
            task_id=task_id,
            context=previous_output,
        )
