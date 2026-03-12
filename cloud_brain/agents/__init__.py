"""
AetherAI — Base Agent
All agents inherit from this. Defines the standard interface.
"""

from abc import ABC, abstractmethod
from typing import Optional

from utils.qwen_client import QwenClient


class BaseAgent(ABC):
    name: str        = "base_agent"
    description: str = "Base agent"

    def __init__(self, qwen: QwenClient, ws_manager=None, **kwargs):
        self.qwen       = qwen
        self.ws_manager = ws_manager

    @abstractmethod
    async def run(
        self,
        parameters: dict,
        task_id: str,
        context: str = "",
    ) -> Optional[str]: ...
    