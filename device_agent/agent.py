"""
AetherAI — Device Agent (Stage 3)
Runs on your PC. Connects to Cloud Brain via WebSocket.
Supports: mouse/keyboard actions, screenshot, vision loop.

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

KEEPALIVE_INTERVAL = 30

# Full paths for Microsoft Office apps (adjust year/version if needed)
OFFICE_PATHS = {
    "word":       r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE",
    "excel":      r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE",
    "powerpoint": r"C:\Program Files\Microsoft Office\root\Office16\POWERPNT.EXE",
    "ppt":        r"C:\Program Files\Microsoft Office\root\Office16\POWERPNT.EXE",
    "winword":    r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE",
}

# Alternative Office paths to try if primary not found
OFFICE_PATHS_ALT = {
    "word":       r"C:\Program Files (x86)\Microsoft Office\root\Office16\WINWORD.EXE",
    "excel":      r"C:\Program Files (x86)\Microsoft Office\root\Office16\EXCEL.EXE",
    "powerpoint": r"C:\Program Files (x86)\Microsoft Office\root\Office16\POWERPNT.EXE",
    "ppt":        r"C:\Program Files (x86)\Microsoft Office\root\Office16\POWERPNT.EXE",
}


def capture_screen(max_width=1280) -> str:
    img = ImageGrab.grab()
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

    # ── Connection loop ───────────────────────────────────────────────────────

    async def connect(self):
        while self.running:
            try:
                logger.info(f"Connecting to {self.ws_url}")
                headers = {"X-Api-Key": API_KEY} if API_KEY else {}
                async with websockets.connect(
                    self.ws_url,
                    additional_headers=headers,
                    ping_interval=None,
                    ping_timeout=None,
                    close_timeout=10,
                ) as ws:
                    logger.info("✓ Connected to AetherAI Cloud Brain")
                    await asyncio.gather(
                        self._listen(ws),
                        self._keepalive(ws),
                    )
            except websockets.ConnectionClosed as e:
                logger.warning(f"Disconnected ({e.code} {e.reason}). Reconnecting in 5s...")
            except Exception as e:
                logger.error(f"Connection error: {e}. Retrying in 5s...")
            await asyncio.sleep(5)

    # ── Keepalive ─────────────────────────────────────────────────────────────

    async def _keepalive(self, ws):
        try:
            while True:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                await ws.send(json.dumps({"type": "ping"}))
                logger.debug("Keepalive ping sent")
        except Exception:
            pass

    # ── Message handler ───────────────────────────────────────────────────────

    async def _listen(self, ws):
        async for raw in ws:
            try:
                data     = json.loads(raw)
                msg_type = data.get("type")

                if msg_type == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
                elif msg_type == "pong":
                    pass
                elif msg_type == "screenshot":
                    await self._handle_screenshot(ws, data)
                elif msg_type == "action":
                    await self._handle_action(ws, data)
                elif msg_type == "vision_task":
                    asyncio.create_task(self._vision_loop(ws, data))
                elif msg_type == "get_screen_info":
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

        aliases = {
            "open":       "open_app",
            "launch":     "open_app",
            "start":      "open_app",
            "press":      "hotkey",
            "key":        "hotkey",
            "write":      "type",
            "typing":     "type",
            "input":      "type",
            "screenshot": "screenshot_and_return",
        }
        action = aliases.get(action, action)

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
                # Use clipboard paste for longer/special text — much faster and more reliable
                if len(text) > 20 or any(ord(c) > 127 for c in text):
                    import pyperclip
                    pyperclip.copy(text)
                    pyautogui.hotkey("ctrl", "v")
                else:
                    pyautogui.write(text, interval=0.04)
                result = f"Typed: {text[:80]}"

            elif action == "type_special":
                import pyperclip
                text = params.get("text", "")
                pyperclip.copy(text)
                pyautogui.hotkey("ctrl", "v")
                result = f"Pasted: {text[:80]}"

            elif action == "hotkey":
                keys = params.get("keys", [])
                pyautogui.hotkey(*keys)
                result = f"Hotkey: {'+'.join(keys)}"

            elif action == "scroll":
                x      = int(params.get("x", pyautogui.size()[0] // 2))
                y      = int(params.get("y", pyautogui.size()[1] // 2))
                clicks = int(params.get("clicks", 3))
                pyautogui.scroll(clicks, x=x, y=y)
                result = f"Scrolled {clicks} at ({x},{y})"

            elif action == "open_app":
                app    = params.get("app", "").strip()
                app_lc = app.lower()
                result = await self._open_app(app, app_lc)

            elif action == "new_file":
                # Open a new blank document in the specified app
                app    = params.get("app", "notepad").strip().lower()
                result = await self._new_file(app)

            elif action == "run_command":
                cmd  = params.get("command", "")
                proc = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=30
                )
                result = (proc.stdout or proc.stderr or "Done").strip()[:500]

            elif action == "wait":
                ms = int(params.get("ms", 1000))
                await asyncio.sleep(ms / 1000)
                result = f"Waited {ms}ms"

            elif action == "screenshot_and_return":
                img_b64 = capture_screen()
                await ws.send(json.dumps({
                    "type":         "screenshot_result",
                    "request_id":   request_id,
                    "image_base64": img_b64,
                    "timestamp":    time.time(),
                }))
                return

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

    # ── App launcher ──────────────────────────────────────────────────────────

    async def _open_app(self, app: str, app_lc: str) -> str:
        """Smart app launcher — tries full Office paths first, then falls back to shell."""

        # Try known Office full paths
        if app_lc in OFFICE_PATHS:
            path = OFFICE_PATHS[app_lc]
            if os.path.exists(path):
                subprocess.Popen([path])
                await asyncio.sleep(3.0)   # Office takes longer to open
                return f"Opened: {app} ({path})"
            # Try alternate path
            alt = OFFICE_PATHS_ALT.get(app_lc)
            if alt and os.path.exists(alt):
                subprocess.Popen([alt])
                await asyncio.sleep(3.0)
                return f"Opened: {app} ({alt})"
            # Fall back to shell start (works if Office is in PATH or registered)
            subprocess.Popen(f'start "" "{app_lc}"', shell=True)
            await asyncio.sleep(3.0)
            return f"Opened (shell): {app}"

        # Standard apps
        if sys.platform == "win32":
            subprocess.Popen(f'start "" "{app}"', shell=True)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-a", app])
        else:
            subprocess.Popen([app])

        await asyncio.sleep(1.5)
        return f"Opened: {app}"

    async def _new_file(self, app: str) -> str:
        """Open a new blank document. For Notepad uses Ctrl+N."""
        if "notepad" in app:
            # Open a fresh Notepad instance directly
            subprocess.Popen(["notepad.exe"])
            await asyncio.sleep(1.0)
            return "Opened new Notepad window"
        elif app in ("word", "winword"):
            path = OFFICE_PATHS.get("word", "")
            if os.path.exists(path):
                subprocess.Popen([path])
                await asyncio.sleep(3.5)
                # Ctrl+N for new document once Word opens
                pyautogui.hotkey("ctrl", "n")
                return "Opened new Word document"
        elif app == "excel":
            path = OFFICE_PATHS.get("excel", "")
            if os.path.exists(path):
                subprocess.Popen([path])
                await asyncio.sleep(3.5)
                return "Opened new Excel workbook"
        elif app in ("powerpoint", "ppt"):
            path = OFFICE_PATHS.get("powerpoint", "")
            if os.path.exists(path):
                subprocess.Popen([path])
                await asyncio.sleep(3.5)
                return "Opened new PowerPoint presentation"
        return f"new_file: unsupported app '{app}'"

    # ── Vision loop ───────────────────────────────────────────────────────────

    async def _vision_loop(self, ws, data: dict):
        goal       = data.get("goal", "")
        task_id    = data.get("task_id", "")
        max_steps  = data.get("max_steps", 10)
        request_id = data.get("request_id", "")

        logger.info(f"Vision loop started. Goal: {goal}")

        for step_num in range(1, max_steps + 1):
            try:
                img_b64 = capture_screen()
                await ws.send(json.dumps({
                    "type":         "vision_step",
                    "task_id":      task_id,
                    "request_id":   request_id,
                    "step":         step_num,
                    "image_base64": img_b64,
                    "goal":         goal,
                }))
                logger.info(f"Vision step {step_num}: screenshot sent, waiting for action...")

                try:
                    response_raw = await asyncio.wait_for(
                        self._wait_for_vision_response(ws, request_id),
                        timeout=30.0
                    )
                except asyncio.TimeoutError:
                    logger.warning("Vision response timed out")
                    break

                response    = json.loads(response_raw)
                action_type = response.get("action")

                if action_type == "done":
                    logger.info(f"Vision goal complete: {response.get('message','')}")
                    await ws.send(json.dumps({
                        "type":        "vision_complete",
                        "task_id":     task_id,
                        "request_id":  request_id,
                        "message":     response.get("message", "Task complete"),
                        "steps_taken": step_num,
                    }))
                    break

                await self._handle_action(ws, {
                    "action":     action_type,
                    "parameters": response.get("parameters", {}),
                    "request_id": "",
                })

                await asyncio.sleep(0.8)

            except Exception as e:
                logger.error(f"Vision loop error at step {step_num}: {e}")
                break

    async def _wait_for_vision_response(self, ws, request_id: str) -> str:
        async for raw in ws:
            data = json.loads(raw)
            if data.get("type") == "vision_action" and data.get("request_id") == request_id:
                return raw
            elif data.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong"}))


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
