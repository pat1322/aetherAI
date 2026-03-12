"""
AetherAI — Device Agent (Stage 3)
Runs on your PC. Connects to Cloud Brain via WebSocket.
Supports: mouse/keyboard actions, screenshot, vision loop (see screen → act).

Usage:
    python agent.py
"""

import asyncio
import base64
import json
import logging
import subprocess
import sys
import time
from io import BytesIO

import websockets
import pyautogui
from PIL import ImageGrab

from config import CLOUD_BRAIN_URL, DEVICE_ID, API_KEY

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DeviceAgent] %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

pyautogui.FAILSAFE = True
pyautogui.PAUSE    = 0.15


def capture_screen(max_width=1280) -> str:
    """Capture screen, resize, return as base64 PNG string."""
    img = ImageGrab.grab()
    # Resize to reduce bandwidth while keeping it readable
    w, h = img.size
    if w > max_width:
        ratio = max_width / w
        img = img.resize((max_width, int(h * ratio)))
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


class DeviceAgent:
    def __init__(self):
        self.ws_url  = f"{CLOUD_BRAIN_URL}/ws/device/{DEVICE_ID}"
        self.running = True
        self._vision_task = None

    # ── Connection loop ───────────────────────────────────────────────────────

    async def connect(self):
        while self.running:
            try:
                logger.info(f"Connecting to {self.ws_url}")
                async with websockets.connect(
                    self.ws_url,
                    extra_headers={"X-Api-Key": API_KEY} if API_KEY else {},
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    logger.info("✓ Connected to AetherAI Cloud Brain")
                    await self._listen(ws)
            except websockets.ConnectionClosed:
                logger.warning("Disconnected. Reconnecting in 5s...")
            except Exception as e:
                logger.error(f"Connection error: {e}. Retrying in 5s...")
            await asyncio.sleep(5)

    # ── Message handler ───────────────────────────────────────────────────────

    async def _listen(self, ws):
        async for raw in ws:
            try:
                data     = json.loads(raw)
                msg_type = data.get("type")

                if msg_type == "ping":
                    await ws.send(json.dumps({"type": "pong"}))

                elif msg_type == "screenshot":
                    await self._handle_screenshot(ws, data)

                elif msg_type == "action":
                    await self._handle_action(ws, data)

                elif msg_type == "vision_task":
                    # Start a vision loop for a complex goal
                    asyncio.create_task(self._vision_loop(ws, data))

                elif msg_type == "get_screen_info":
                    # Return screen size
                    w, h = pyautogui.size()
                    await ws.send(json.dumps({
                        "type": "screen_info",
                        "width": w, "height": h,
                        "request_id": data.get("request_id", ""),
                    }))

                else:
                    logger.info(f"Unknown message: {msg_type}")

            except Exception as e:
                logger.error(f"Message handling error: {e}")

    # ── Screenshot ────────────────────────────────────────────────────────────

    async def _handle_screenshot(self, ws, data: dict):
        request_id = data.get("request_id", "")
        try:
            img_b64 = capture_screen()
            await ws.send(json.dumps({
                "type":         "screenshot_result",
                "request_id":   request_id,
                "image_base64": img_b64,
                "timestamp":    time.time(),
            }))
            logger.info("Screenshot sent")
        except Exception as e:
            logger.error(f"Screenshot error: {e}")
            await ws.send(json.dumps({
                "type": "screenshot_result",
                "request_id": request_id,
                "error": str(e),
            }))

    # ── Single action executor ────────────────────────────────────────────────

    async def _handle_action(self, ws, data: dict):
        action     = data.get("action")
        params     = data.get("parameters", {})
        request_id = data.get("request_id", "")
        result     = "ok"

        try:
            if action == "click":
                x, y = int(params["x"]), int(params["y"])
                pyautogui.click(x, y)
                result = f"Clicked ({x}, {y})"

            elif action == "double_click":
                x, y = int(params["x"]), int(params["y"])
                pyautogui.doubleClick(x, y)
                result = f"Double-clicked ({x}, {y})"

            elif action == "right_click":
                x, y = int(params["x"]), int(params["y"])
                pyautogui.rightClick(x, y)
                result = f"Right-clicked ({x}, {y})"

            elif action == "move":
                x, y = int(params["x"]), int(params["y"])
                pyautogui.moveTo(x, y, duration=0.3)
                result = f"Moved to ({x}, {y})"

            elif action == "type":
                text = params.get("text", "")
                pyautogui.write(text, interval=0.04)
                result = f"Typed: {text[:60]}"

            elif action == "type_special":
                # For non-ASCII text, use clipboard paste
                import pyperclip
                text = params.get("text", "")
                pyperclip.copy(text)
                pyautogui.hotkey("ctrl", "v")
                result = f"Pasted: {text[:60]}"

            elif action == "hotkey":
                keys = params.get("keys", [])
                pyautogui.hotkey(*keys)
                result = f"Hotkey: {'+'.join(keys)}"

            elif action == "scroll":
                x    = int(params.get("x", pyautogui.size()[0] // 2))
                y    = int(params.get("y", pyautogui.size()[1] // 2))
                clicks = int(params.get("clicks", 3))
                pyautogui.scroll(clicks, x=x, y=y)
                result = f"Scrolled {clicks} at ({x},{y})"

            elif action == "open_app":
                app = params.get("app", "")
                if sys.platform == "win32":
                    subprocess.Popen(f'start "" "{app}"', shell=True)
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", "-a", app])
                else:
                    subprocess.Popen([app])
                await asyncio.sleep(1.5)   # wait for app to open
                result = f"Opened: {app}"

            elif action == "run_command":
                cmd = params.get("command", "")
                proc = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=30
                )
                result = (proc.stdout or proc.stderr or "Done").strip()[:500]

            elif action == "wait":
                ms = int(params.get("ms", 1000))
                await asyncio.sleep(ms / 1000)
                result = f"Waited {ms}ms"

            elif action == "screenshot_and_return":
                # Take screenshot and include it in result
                img_b64 = capture_screen()
                await ws.send(json.dumps({
                    "type":         "screenshot_result",
                    "request_id":   request_id,
                    "image_base64": img_b64,
                    "timestamp":    time.time(),
                }))
                return  # already sent response

            else:
                result = f"Unknown action: {action}"

            logger.info(f"✓ {action}: {result[:80]}")

        except Exception as e:
            result = f"Error: {e}"
            logger.error(f"Action '{action}' failed: {e}")

        if request_id:
            await ws.send(json.dumps({
                "type":       "action_result",
                "request_id": request_id,
                "result":     result,
            }))

    # ── Vision loop ───────────────────────────────────────────────────────────

    async def _vision_loop(self, ws, data: dict):
        """
        Stage 3 vision loop:
        1. Take screenshot
        2. Send to Cloud Brain for Qwen to analyze
        3. Receive next action
        4. Execute action
        5. Repeat until goal is complete or max steps reached
        """
        goal       = data.get("goal", "")
        task_id    = data.get("task_id", "")
        max_steps  = data.get("max_steps", 10)
        request_id = data.get("request_id", "")

        logger.info(f"Vision loop started. Goal: {goal}")

        for step_num in range(1, max_steps + 1):
            try:
                # Capture current screen state
                img_b64 = capture_screen()

                # Send screenshot + goal to Cloud Brain for analysis
                await ws.send(json.dumps({
                    "type":         "vision_step",
                    "task_id":      task_id,
                    "request_id":   request_id,
                    "step":         step_num,
                    "image_base64": img_b64,
                    "goal":         goal,
                }))

                logger.info(f"Vision step {step_num}: screenshot sent, waiting for action...")

                # Wait for cloud brain to respond with next action
                # (with timeout)
                try:
                    response_raw = await asyncio.wait_for(
                        self._wait_for_vision_response(ws, request_id),
                        timeout=30.0
                    )
                except asyncio.TimeoutError:
                    logger.warning("Vision response timed out")
                    break

                response = json.loads(response_raw)
                action_type = response.get("action")

                # Goal complete
                if action_type == "done":
                    logger.info(f"Vision goal complete: {response.get('message','')}")
                    await ws.send(json.dumps({
                        "type":       "vision_complete",
                        "task_id":    task_id,
                        "request_id": request_id,
                        "message":    response.get("message", "Task complete"),
                        "steps_taken": step_num,
                    }))
                    break

                # Execute the action
                await self._handle_action(ws, {
                    "action":     action_type,
                    "parameters": response.get("parameters", {}),
                    "request_id": "",  # no individual ack needed in vision loop
                })

                # Small pause between actions
                await asyncio.sleep(0.8)

            except Exception as e:
                logger.error(f"Vision loop error at step {step_num}: {e}")
                break

    async def _wait_for_vision_response(self, ws, request_id: str) -> str:
        """Wait for a vision_action message matching our request_id."""
        async for raw in ws:
            data = json.loads(raw)
            if data.get("type") == "vision_action" and data.get("request_id") == request_id:
                return raw
            elif data.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong"}))
            # Keep processing other messages while waiting


async def main():
    logger.info(f"AetherAI Device Agent v3 — Device ID: {DEVICE_ID}")
    logger.info("Press Ctrl+C to stop")
    agent = DeviceAgent()
    try:
        await agent.connect()
    except KeyboardInterrupt:
        logger.info("Device Agent stopped.")


if __name__ == "__main__":
    asyncio.run(main())
