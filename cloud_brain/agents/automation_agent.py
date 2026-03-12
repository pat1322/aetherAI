"""
AetherAI — Automation Agent (Stage 1 stub)
Forwards mouse/keyboard commands to the Device Agent via WebSocket.
Full implementation in Stage 3.
"""
import logging
from agents import BaseAgent
from utils.websocket_manager import WebSocketManager
logger = logging.getLogger(__name__)

class AutomationAgent(BaseAgent):
    name = "automation_agent"
    description = "Controls mouse and keyboard on connected devices"

    def __init__(self, qwen, ws_manager: WebSocketManager):
        super().__init__(qwen)
        self.ws_manager = ws_manager

    async def run(self, parameters: dict, task_id: str, context: str = "") -> str:
        action = parameters.get("action", "unknown")
        device_count = self.ws_manager.device_count()
        if device_count == 0:
            return "[AutomationAgent] No device agents connected. Start the device_agent on your PC."
        result = await self.ws_manager.send_to_any_device({
            "type": "action",
            "action": action,
            "parameters": parameters,
            "task_id": task_id,
        })
        return f"[AutomationAgent] Action '{action}' sent to device. Result: {result}"
