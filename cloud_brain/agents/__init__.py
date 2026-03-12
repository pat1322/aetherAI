"""
AetherAI — Base Agent
All agents inherit from this. Defines the standard interface.
"""

from abc import ABC, abstractmethod
from typing import Optional

from utils.qwen_client import QwenClient


class BaseAgent(ABC):
    """
    Every agent must implement the `run` method.
    Agents receive parameters + context from the Orchestrator
    and return a string output (result, summary, file path, etc.).
    """

    name: str = "base_agent"
    description: str = "Base agent"

    def __init__(self, qwen: QwenClient):
        self.qwen = qwen

    @abstractmethod
    async def run(
        self,
        parameters: dict,
        task_id: str,
        context: str = "",
    ) -> Optional[str]:
        """
        Execute a step.

        Args:
            parameters: Dict of inputs for this step (from the plan)
            task_id:    ID of the parent task (for logging/memory)
            context:    Output from the previous step (chained tasks)

        Returns:
            String result (summary, file path, code output, etc.)
        """
        ...
