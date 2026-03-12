"""
AetherAI — WebSocket Manager (Stage 3)
Manages connections for Device Agents and Web UI sessions.
Supports: task broadcasts, pending request/response futures, vision task handlers.
"""

import asyncio
import json
import logging
from typing import Any, Callable, Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WebSocketManager:
    def __init__(self):
        self._devices:      dict[str, WebSocket] = {}   # device_id → ws
        self._ui_sessions:  dict[str, WebSocket] = {}   # session_id → ws
        self._pending:      dict[str, asyncio.Future] = {}  # request_id → Future
        self._vision_handlers: dict[str, Callable] = {}  # request_id → handler fn

    # ── Device connections ────────────────────────────────────────────────────

    async def connect_device(self, device_id: str, ws: WebSocket):
        await ws.accept()
        self._devices[device_id] = ws
        logger.info(f"Device connected: {device_id}")
        await self.broadcast_ui_event({"type": "device_connected", "device_id": device_id})

    def disconnect_device(self, device_id: str):
        self._devices.pop(device_id, None)
        logger.info(f"Device disconnected: {device_id}")
        asyncio.create_task(
            self.broadcast_ui_event({"type": "device_disconnected", "device_id": device_id})
        )

    def list_devices(self) -> list[str]:
        return list(self._devices.keys())

    def device_count(self) -> int:
        return len(self._devices)

    async def send_to_device(self, device_id: str, data: dict) -> bool:
        ws = self._devices.get(device_id)
        if not ws:
            logger.warning(f"Device {device_id} not connected")
            return False
        try:
            await ws.send_text(json.dumps(data))
            return True
        except Exception as e:
            logger.error(f"Error sending to device {device_id}: {e}")
            self.disconnect_device(device_id)
            return False

    # ── UI sessions ───────────────────────────────────────────────────────────

    async def connect_ui(self, session_id: str, ws: WebSocket):
        await ws.accept()
        self._ui_sessions[session_id] = ws
        logger.info(f"UI session connected: {session_id}")

    def disconnect_ui(self, session_id: str):
        self._ui_sessions.pop(session_id, None)

    async def broadcast_ui_event(self, data: dict):
        """Send an event to all connected UI sessions."""
        dead = []
        for sid, ws in self._ui_sessions.items():
            try:
                await ws.send_text(json.dumps(data))
            except Exception:
                dead.append(sid)
        for sid in dead:
            self._ui_sessions.pop(sid, None)

    async def broadcast_task_update(self, task_id: str, data: dict):
        """Broadcast a task progress update to all UI sessions."""
        payload = {"type": "task_update", "task_id": task_id, **data}
        await self.broadcast_ui_event(payload)

    # ── Pending request/response (for actions that need a reply) ─────────────

    def register_pending(self, request_id: str, future: asyncio.Future):
        self._pending[request_id] = future

    def unregister_pending(self, request_id: str):
        self._pending.pop(request_id, None)

    def register_vision_task(self, request_id: str, future: asyncio.Future,
                              handler: Callable):
        self._pending[request_id] = future
        self._vision_handlers[request_id] = handler

    async def handle_device_message(self, device_id: str, data: dict):
        """Route messages coming FROM the device."""
        msg_type   = data.get("type")
        request_id = data.get("request_id", "")

        # Action result — resolve the waiting future
        if msg_type == "action_result" and request_id in self._pending:
            future = self._pending.get(request_id)
            if future and not future.done():
                future.set_result(data)

        # Screenshot result — resolve the waiting future
        elif msg_type == "screenshot_result" and request_id in self._pending:
            future = self._pending.get(request_id)
            if future and not future.done():
                future.set_result(data)

        # Vision step — device sent a screenshot for analysis
        elif msg_type == "vision_step" and request_id in self._vision_handlers:
            handler = self._vision_handlers[request_id]
            try:
                action = await handler(device_id, request_id, data)
                # Send the action back to the device
                await self.send_to_device(device_id, {
                    "type":       "vision_action",
                    "request_id": request_id,
                    **action,
                })
            except Exception as e:
                logger.error(f"Vision handler error: {e}")
                await self.send_to_device(device_id, {
                    "type":       "vision_action",
                    "request_id": request_id,
                    "action":     "done",
                    "message":    f"Error: {e}",
                })

        # Vision complete — resolve the waiting future
        elif msg_type == "vision_complete" and request_id in self._pending:
            future = self._pending.get(request_id)
            if future and not future.done():
                future.set_result(data.get("message", "Done"))
            self._vision_handlers.pop(request_id, None)

        # Screen info response
        elif msg_type == "screen_info" and request_id in self._pending:
            future = self._pending.get(request_id)
            if future and not future.done():
                future.set_result(data)

        # Forward device status updates to UI
        elif msg_type in ("status", "log", "error"):
            await self.broadcast_ui_event({
                "type":      "device_log",
                "device_id": device_id,
                "message":   data.get("message", ""),
                "level":     data.get("level", "info"),
            })
