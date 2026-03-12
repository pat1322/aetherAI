"""
AetherAI — Device Agent
Runs on your PC/laptop. Connects to Cloud Brain via WebSocket.
Executes mouse, keyboard, screenshot commands.

Usage:
    python agent.py
"""

import asyncio
import base64
import json
import logging
import os
import subprocess
import sys
from io import BytesIO

import websockets
import pyautogui
from PIL import Image, ImageGrab

from config import CLOUD_BRAIN_URL, DEVICE_ID, API_KEY

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DeviceAgent] %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

# Safety: disable pyautogui failsafe (move mouse to corner to abort)
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.1   # Small delay between actions


class DeviceAgent:
    def __init__(self):
        self.ws_url = f"{CLOUD_BRAIN_URL}/ws/device/{DEVICE_ID}"
        self.running = True

    async def connect(self):
        """Main loop: connect to cloud, handle reconnects."""
        while self.running:
            try:
                logger.info(f"Connecting to Cloud Brain at {self.ws_url}")
                async with websockets.connect(
                    self.ws_url,
                    extra_headers={"X-Api-Key": API_KEY} if API_KEY else {},
                    ping_interval=30,
                    ping_timeout=10,
                ) as ws:
                    logger.info("Connected to Cloud Brain ✓")
                    await self._listen(ws)
            except websockets.ConnectionClosed:
                logger.warning("Connection closed. Reconnecting in 5s...")
            except Exception as e:
                logger.error(f"Connection error: {e}. Retrying in 5s...")
            await asyncio.sleep(5)

    async def _listen(self, ws):
        """Listen for commands from the Cloud Brain."""
        async for raw in ws:
            try:
                data = json.loads(raw)
                msg_type = data.get("type")

                if msg_type == "ping":
                    await ws.send(json.dumps({"type": "pong"}))

                elif msg_type == "screenshot":
                    await self._handle_screenshot(ws, data)

                elif msg_type == "action":
                    await self._handle_action(ws, data)

                else:
                    logger.info(f"Unknown message type: {msg_type}")

            except Exception as e:
                logger.error(f"Error handling message: {e}")

    async def _handle_screenshot(self, ws, data: dict):
        """Capture screen and send back as base64."""
        request_id = data.get("request_id", "")
        try:
            screenshot = ImageGrab.grab()
            # Resize for bandwidth efficiency
            screenshot.thumbnail((1920, 1080))
            buffer = BytesIO()
            screenshot.save(buffer, format="PNG", optimize=True)
            img_b64 = base64.b64encode(buffer.getvalue()).decode()

            await ws.send(json.dumps({
                "type": "screenshot_result",
                "request_id": request_id,
                "image_base64": img_b64,
            }))
            logger.info("Screenshot sent")
        except Exception as e:
            logger.error(f"Screenshot error: {e}")
            await ws.send(json.dumps({
                "type": "screenshot_result",
                "request_id": request_id,
                "error": str(e),
            }))

    async def _handle_action(self, ws, data: dict):
        """Execute a GUI action."""
        action = data.get("action")
        params = data.get("parameters", {})
        request_id = data.get("request_id", "")
        result = "ok"

        try:
            if action == "click":
                x, y = params.get("x", 0), params.get("y", 0)
                pyautogui.click(x, y)
                result = f"Clicked ({x}, {y})"

            elif action == "double_click":
                x, y = params.get("x", 0), params.get("y", 0)
                pyautogui.doubleClick(x, y)
                result = f"Double-clicked ({x}, {y})"

            elif action == "right_click":
                x, y = params.get("x", 0), params.get("y", 0)
                pyautogui.rightClick(x, y)
                result = f"Right-clicked ({x}, {y})"

            elif action == "move":
                x, y = params.get("x", 0), params.get("y", 0)
                pyautogui.moveTo(x, y, duration=0.2)
                result = f"Moved to ({x}, {y})"

            elif action == "type":
                text = params.get("text", "")
                pyautogui.write(text, interval=0.05)
                result = f"Typed: {text[:50]}"

            elif action == "hotkey":
                keys = params.get("keys", [])
                pyautogui.hotkey(*keys)
                result = f"Hotkey: {'+'.join(keys)}"

            elif action == "scroll":
                x, y = params.get("x", 0), params.get("y", 0)
                clicks = params.get("clicks", 3)
                pyautogui.scroll(clicks, x=x, y=y)
                result = f"Scrolled {clicks} at ({x}, {y})"

            elif action == "open_app":
                app = params.get("app", "")
                if sys.platform == "win32":
                    subprocess.Popen(["start", app], shell=True)
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", "-a", app])
                else:
                    subprocess.Popen([app])
                result = f"Opened: {app}"

            elif action == "run_command":
                cmd = params.get("command", "")
                proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
                result = proc.stdout or proc.stderr or "Command executed"

            else:
                result = f"Unknown action: {action}"
                logger.warning(result)

            logger.info(f"Action '{action}': {result}")

        except Exception as e:
            result = f"Error: {str(e)}"
            logger.error(f"Action '{action}' failed: {e}")

        if request_id:
            await ws.send(json.dumps({
                "type": "action_result",
                "request_id": request_id,
                "result": result,
            }))


async def main():
    agent = DeviceAgent()
    logger.info(f"AetherAI Device Agent starting. Device ID: {DEVICE_ID}")
    await agent.connect()


if __name__ == "__main__":
    asyncio.run(main())
