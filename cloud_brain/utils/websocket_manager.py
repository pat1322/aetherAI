"""
AetherAI — WebSocket Manager
Manages persistent connections from Device Agents and Web UI sessions.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WebSocketManager:
    """
    Tracks two pools of WebSocket connections:
    - device_connections: Device Agents (your PC/laptop)
    - ui_connections:     Web UI browser sessions
    """

    def __init__(self):
        self.device_connections: Dict[str, WebSocket] = {}
        self.ui_connections: Dict[str, WebSocket] = {}
        self._pending_responses: Dict[str, asyncio.Future] = {}

    # ── Device Agent connections ──────────────────────────────────────────────

    async def connect_device(self, device_id: str, websocket: WebSocket):
        await websocket.accept()
        self.device_connections[device_id] = websocket
        logger.info(f"Device connected: {device_id}")
        await self._broadcast_ui({
            "type": "device_connected",
            "device_id": device_id,
            "timestamp": datetime.utcnow().isoformat(),
        })

    def disconnect_device(self, device_id: str):
        self.device_connections.pop(device_id, None)
        logger.info(f"Device disconnected: {device_id}")

    async def send_to_device(self, device_id: str, message: dict) -> bool:
        """Send an action command to a specific device. Returns True if sent."""
        ws = self.device_connections.get(device_id)
        if not ws:
            logger.warning(f"Device {device_id} not connected")
            return False
        try:
            await ws.send_text(json.dumps(message))
            return True
        except Exception as e:
            logger.error(f"Failed to send to device {device_id}: {e}")
            self.disconnect_device(device_id)
            return False

    async def send_to_any_device(self, message: dict) -> bool:
        """Send to the first available device. Returns True if sent."""
        for device_id in list(self.device_connections.keys()):
            if await self.send_to_device(device_id, message):
                return True
        return False

    async def request_screenshot(self, device_id: Optional[str] = None) -> Optional[str]:
        """
        Request a screenshot from a device and wait for the response.
        Returns base64-encoded image string or None on timeout.
        """
        request_id = f"screenshot_{datetime.utcnow().timestamp()}"
        message = {"type": "screenshot", "request_id": request_id}

        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_responses[request_id] = future

        sent = (
            await self.send_to_device(device_id, message)
            if device_id
            else await self.send_to_any_device(message)
        )

        if not sent:
            self._pending_responses.pop(request_id, None)
            return None

        try:
            result = await asyncio.wait_for(future, timeout=10.0)
            return result
        except asyncio.TimeoutError:
            logger.warning("Screenshot request timed out")
            self._pending_responses.pop(request_id, None)
            return None

    async def handle_device_message(self, device_id: str, data: dict):
        """Process incoming messages from a Device Agent."""
        msg_type = data.get("type")

        if msg_type == "screenshot_result":
            request_id = data.get("request_id")
            future = self._pending_responses.pop(request_id, None)
            if future and not future.done():
                future.set_result(data.get("image_base64"))

        elif msg_type == "action_result":
            request_id = data.get("request_id")
            future = self._pending_responses.pop(request_id, None)
            if future and not future.done():
                future.set_result(data.get("result"))

        elif msg_type == "heartbeat":
            logger.debug(f"Heartbeat from device {device_id}")

        else:
            logger.info(f"Device {device_id} message: {data}")

    def device_count(self) -> int:
        return len(self.device_connections)

    def list_devices(self) -> list:
        return [
            {"device_id": did, "status": "connected"}
            for did in self.device_connections.keys()
        ]

    # ── Web UI connections ────────────────────────────────────────────────────

    async def connect_ui(self, session_id: str, websocket: WebSocket):
        await websocket.accept()
        self.ui_connections[session_id] = websocket
        logger.info(f"UI session connected: {session_id}")

    def disconnect_ui(self, session_id: str):
        self.ui_connections.pop(session_id, None)

    async def send_to_ui(self, session_id: str, message: dict):
        ws = self.ui_connections.get(session_id)
        if ws:
            try:
                await ws.send_text(json.dumps(message))
            except Exception:
                self.disconnect_ui(session_id)

    async def _broadcast_ui(self, message: dict):
        """Broadcast a message to all connected UI sessions."""
        dead = []
        for session_id, ws in self.ui_connections.items():
            try:
                await ws.send_text(json.dumps(message))
            except Exception:
                dead.append(session_id)
        for s in dead:
            self.disconnect_ui(s)

    async def broadcast_task_update(self, task_id: str, update: dict):
        """Broadcast a task progress update to all UI sessions."""
        await self._broadcast_ui({
            "type": "task_update",
            "task_id": task_id,
            **update,
            "timestamp": datetime.utcnow().isoformat(),
        })
