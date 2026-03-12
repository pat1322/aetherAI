"""
AetherAI — Coding Agent (Stage 1 stub)
"""
import logging
from agents import BaseAgent
logger = logging.getLogger(__name__)

class CodingAgent(BaseAgent):
    name = "coding_agent"
    description = "Writes and executes code"

    async def run(self, parameters: dict, task_id: str, context: str = "") -> str:
        task = parameters.get("task") or context
        logger.info(f"[CodingAgent] Task: {task}")
        code = await self.qwen.chat(
            system_prompt="You are an expert programmer. Write clean, well-commented code.",
            user_message=f"Write code for: {task}",
        )
        return f"[CodingAgent] Generated code:\n\n{code}"
