"""
AetherAI — Device Agent (Stage 3)
Runs on your PC. Connects to Cloud Brain via WebSocket.

FIXES:
- Office apps launched with /n (Word), /e (Excel), /blank (PowerPoint) — opens directly
  to a blank document, no start screen, NO double-window issue
- Window focus via PowerShell AppActivate before any type action
- Notepad++ supported with known install paths
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

OFFICE_APPS = {
    "word":        ["WINWORD.EXE",  "winword"],
    "excel":       ["EXCEL.EXE",    "excel"],
    "powerpoint":  ["POWERPNT.EXE", "powerpnt"],
    "ppt":         ["POWERPNT.EXE", "powerpnt"],
}

OFFICE_BASE_PATHS = [
    r"C:\Program Files\Microsoft Office\root\Office16",
    r"C:\Program Files (x86)\Microsoft Office\root\Office16",
    r"C:\Program Files\Microsoft Office\root\Office15",
    r"C:\Program Files (x86)\Microsoft Office\root\Office15",
    r"C:\Program Files\Microsoft Office 16\root\Office16",
]

# Launch flags that bypass the Office start screen and open a blank file directly
# This prevents the double-window issue caused by Ctrl+N on an already-open blank doc
OFFICE_NEW_FLAGS = {
    "word":       ["/n"],      # new blank document, no start screen
    "excel":      ["/e"],      # skip startup dialog
    "powerpoint": ["/blank"],  # new blank presentation
}

NOTEPADPP_PATHS = [
    r"C:\Program Files\Notepad++\notepad++.exe",
    r"C:\Program Files (x86)\Notepad++\notepad++.exe",
    r"C:\Users\patri\AppData\Local\Programs\Notepad++\notepad++.exe",
    r"C:\Users\patri\scoop\apps\notepadplusplus\current\notepad++.exe",
]


def find_office_exe(app_key: str) -> str | None:
    exes = OFFICE_APPS.get(app_key.lower(), [])
    for base in OFFICE_BASE_PATHS:
        for exe in exes:
            full = os.path.join(base, exe)
            if os.path.exists(full):
                return full
    return None


def find_notepadpp() -> str | None:
    for path in NOTEPADPP_PATHS:
        if os.path.exists(path):
            return path
    return None


def activate_window_by_title(title_fragment: str):
    """Bring a window to the foreground using PowerShell (no extra packages needed)."""
    try:
        ps_cmd = (
            "Add-Type -AssemblyName Microsoft.VisualBasic; "
            f"[Microsoft.VisualBasic.Interaction]::AppActivate('{title_fragment}')"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, timeout=5
        )
    except Exception as e:
        logger.warning(f"activate_window_by_title('{title_fragment}') failed: {e}")


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

    async def _keepalive(self, ws):
        try:
            while True:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                await ws.send(json.dumps({"type": "ping"}))
        except Exception:
            pass

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
                        "type": "screen_info", "width": w, "height": h,
                        "request_id": data.get("request_id", ""),
                    }))
                else:
                    logger.info(f"Unknown message: {msg_type}")
            except Exception as e:
                logger.error(f"Message handling error: {e}")

    async def _handle_screenshot(self, ws, data: dict):
        request_id = data.get("request_id", "")
        try:
            img_b64 = capture_screen()
            await ws.send(json.dumps({
                "type": "screenshot_result", "request_id": request_id,
                "image_base64": img_b64, "timestamp": time.time(),
            }))
        except Exception as e:
            await ws.send(json.dumps({
                "type": "screenshot_result", "request_id": request_id, "error": str(e)
            }))

    async def _handle_action(self, ws, data: dict):
        action     = data.get("action")
        params     = data.get("parameters", {})
        request_id = data.get("request_id", "")
        result     = "ok"

        aliases = {
            "open": "open_app", "launch": "open_app", "start": "open_app",
            "press": "hotkey", "key": "hotkey",
            "write": "type", "typing": "type", "input": "type",
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
                if not text:
                    result = "type: no text provided"
                else:
                    import pyperclip
                    pyperclip.copy(text)
                    await asyncio.sleep(0.2)
                    pyautogui.hotkey("ctrl", "v")
                    await asyncio.sleep(0.3)
                    result = f"Typed {len(text)} chars"

            elif action == "type_special":
                import pyperclip
                text = params.get("text", "")
                pyperclip.copy(text)
                pyautogui.hotkey("ctrl", "v")
                result = f"Pasted: {text[:120]}"

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
                app = params.get("app", "").strip()
                result = await self._open_app(app)

            elif action == "new_file":
                app = params.get("app", "notepad").strip().lower()
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
                    "type": "screenshot_result", "request_id": request_id,
                    "image_base64": img_b64, "timestamp": time.time(),
                }))
                return

            else:
                result = f"Unknown action: {action}"

            logger.info(f"✓ {action}: {result[:100]}")

        except Exception as e:
            result = f"Error: {e}"
            logger.error(f"Action '{action}' failed: {e}")

        if request_id:
            await ws.send(json.dumps({
                "type": "action_result", "request_id": request_id, "result": result,
            }))

    async def _open_app(self, app: str) -> str:
        app_lc = app.lower().strip()

        if "notepad++" in app_lc or "notepadpp" in app_lc:
            npp_path = find_notepadpp()
            if npp_path:
                subprocess.Popen([npp_path])
                await asyncio.sleep(2.5)
                activate_window_by_title("Notepad++")
                await asyncio.sleep(0.4)
                return f"Opened Notepad++ ({npp_path})"
            subprocess.Popen('start "" "notepad++"', shell=True)
            await asyncio.sleep(2.0)
            return "Opened Notepad++ (shell)"

        office_path = find_office_exe(app_lc)
        if office_path:
            subprocess.Popen([office_path])
            await asyncio.sleep(4.0)
            return f"Opened: {app} ({office_path})"

        if sys.platform == "win32":
            subprocess.Popen(f'start "" "{app}"', shell=True)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-a", app])
        else:
            subprocess.Popen([app])
        await asyncio.sleep(1.5)
        return f"Opened: {app}"

    async def _new_file(self, app: str) -> str:
        """Open a blank file using Office flags — avoids double-window problem."""
        app_lc = app.lower().strip()

        # ── Notepad ───────────────────────────────────────────────────────────
        if app_lc == "notepad":
            subprocess.Popen(["notepad.exe"])
            await asyncio.sleep(1.5)
            activate_window_by_title("Notepad")
            await asyncio.sleep(0.4)
            screen_w, screen_h = pyautogui.size()
            pyautogui.click(screen_w // 2, screen_h // 2)
            await asyncio.sleep(0.2)
            return "Opened new Notepad window"

        # ── Notepad++ ─────────────────────────────────────────────────────────
        if "notepad++" in app_lc or "notepadpp" in app_lc:
            npp_path = find_notepadpp()
            if npp_path:
                subprocess.Popen([npp_path])
                await asyncio.sleep(2.5)
                activate_window_by_title("Notepad++")
                await asyncio.sleep(0.4)
                pyautogui.hotkey("ctrl", "n")
                await asyncio.sleep(0.5)
                screen_w, screen_h = pyautogui.size()
                pyautogui.click(screen_w // 2, screen_h // 2)
                await asyncio.sleep(0.2)
                return "Opened new Notepad++ window"
            logger.warning("Notepad++ not found — falling back to Notepad")
            subprocess.Popen(["notepad.exe"])
            await asyncio.sleep(1.5)
            activate_window_by_title("Notepad")
            await asyncio.sleep(0.4)
            screen_w, screen_h = pyautogui.size()
            pyautogui.click(screen_w // 2, screen_h // 2)
            await asyncio.sleep(0.2)
            return "Opened new Notepad window (Notepad++ not found)"

        # ── Microsoft Office ──────────────────────────────────────────────────
        office_map = {
            "word":       "word",
            "winword":    "word",
            "excel":      "excel",
            "powerpoint": "powerpoint",
            "ppt":        "powerpoint",
        }
        office_titles = {
            "word":       "Word",
            "excel":      "Excel",
            "powerpoint": "PowerPoint",
        }
        office_body_y = {
            "word":       0.55,
            "excel":      0.50,
            "powerpoint": 0.55,
        }

        office_key = office_map.get(app_lc)
        if office_key:
            office_path = find_office_exe(office_key)
            if not office_path:
                return f"⚠️ {office_key} not found. Check Office installation path."

            # Use /n, /e, or /blank to open directly to a blank document.
            # This skips the start screen entirely so Ctrl+N is NOT needed,
            # which was causing a second window to open.
            new_flags = OFFICE_NEW_FLAGS.get(office_key, [])
            subprocess.Popen([office_path] + new_flags)
            logger.info(f"Launched {office_key} with flags {new_flags}, waiting 5s...")
            await asyncio.sleep(5.0)

            title_hint = office_titles.get(office_key, office_key.title())
            activate_window_by_title(title_hint)
            await asyncio.sleep(0.6)

            # Click into the document body to ensure keyboard focus
            screen_w, screen_h = pyautogui.size()
            body_y = office_body_y.get(office_key, 0.55)
            pyautogui.click(screen_w // 2, int(screen_h * body_y))
            await asyncio.sleep(0.3)

            return f"Opened new {office_key} document"

        return f"new_file: unsupported app '{app}'"

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
                    "type": "vision_step", "task_id": task_id,
                    "request_id": request_id, "step": step_num,
                    "image_base64": img_b64, "goal": goal,
                }))
                try:
                    response_raw = await asyncio.wait_for(
                        self._wait_for_vision_response(ws, request_id), timeout=30.0
                    )
                except asyncio.TimeoutError:
                    logger.warning("Vision response timed out")
                    break

                response    = json.loads(response_raw)
                action_type = response.get("action")
                if action_type == "done":
                    await ws.send(json.dumps({
                        "type": "vision_complete", "task_id": task_id,
                        "request_id": request_id,
                        "message": response.get("message", "Task complete"),
                        "steps_taken": step_num,
                    }))
                    break

                await self._handle_action(ws, {
                    "action": action_type,
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
