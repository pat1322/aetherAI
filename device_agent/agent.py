"""
AetherAI — Device Agent  (Stage 4 — hardened, patched)

Fixes applied
─────────────
FIX 5  "Open Chrome" was failing because subprocess was called with just
       "chrome" (not the full path). Added CHROME_PATHS with common Windows
       install locations and a find_chrome() helper. _open_app() now tries the
       resolved path before falling back to the generic OS open.

FIX 6  When Notepad (or another text editor) was opened and then a type action
       immediately followed, the typing arrived before the window was focused.
       The type action handler now calls activate_window_by_title() for the
       last known app name (passed as an optional "activate_app" param) and
       waits 400 ms before sending keystrokes. If no app hint is provided a
       general 500 ms pause + neutral focus re-assertion is applied.
"""

import asyncio
import base64
import configparser
import json
import logging
import os
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path

import websockets
import pyautogui
from PIL import Image, ImageGrab

# ── Config loading ─────────────────────────────────────────────────────────────

def _load_config():
    if getattr(sys, "frozen", False):
        base_dir = Path(sys.executable).parent
    else:
        base_dir = Path(__file__).parent

    ini_path = base_dir / "aether_config.ini"

    if ini_path.exists():
        cfg = configparser.ConfigParser()
        cfg.read(ini_path)
        return (
            cfg.get("aether", "cloud_url",  fallback="wss://aetherai.up.railway.app"),
            cfg.get("aether", "device_id",  fallback="device-" + str(os.getpid())),
            cfg.get("aether", "api_key",    fallback=""),
        )
    else:
        try:
            from config import CLOUD_BRAIN_URL, DEVICE_ID, API_KEY
            return CLOUD_BRAIN_URL, DEVICE_ID, API_KEY
        except ImportError:
            return "wss://aetherai.up.railway.app", "device-unknown", ""


CLOUD_BRAIN_URL, DEVICE_ID, API_KEY = _load_config()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DeviceAgent] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

pyautogui.FAILSAFE = True
pyautogui.PAUSE    = 0.1

KEEPALIVE_INTERVAL  = 30
MAX_ACTION_RETRIES  = 1
MAX_RECONNECT_DELAY = 30

# ── Notepad++ search paths ─────────────────────────────────────────────────────

NOTEPADPP_PATHS = [
    r"C:\Program Files\Notepad++\notepad++.exe",
    r"C:\Program Files (x86)\Notepad++\notepad++.exe",
    r"C:\Users\patri\AppData\Local\Programs\Notepad++\notepad++.exe",
    r"C:\Users\patri\scoop\apps\notepadplusplus\current\notepad++.exe",
]

# FIX 5: Chrome executable paths for common Windows install locations
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Users\patri\AppData\Local\Google\Chrome\Application\chrome.exe",
    r"C:\Users\Public\Desktop\Google Chrome.lnk",
]

# ── Office COM scripts ─────────────────────────────────────────────────────────

OFFICE_COM_SCRIPTS = {
    "word": """
try {
    $app = [System.Runtime.InteropServices.Marshal]::GetActiveObject('Word.Application')
} catch {
    $app = New-Object -ComObject Word.Application
}
$app.Visible = $true
$doc = $app.Documents.Add()
$app.Activate()
[System.Runtime.InteropServices.Marshal]::ReleaseComObject($doc) | Out-Null
[System.Runtime.InteropServices.Marshal]::ReleaseComObject($app) | Out-Null
""",
    "excel": """
try {
    $app = [System.Runtime.InteropServices.Marshal]::GetActiveObject('Excel.Application')
} catch {
    $app = New-Object -ComObject Excel.Application
}
$app.Visible = $true
$wb = $app.Workbooks.Add()
$app.WindowState = -4137
$app.Activate()
[System.Runtime.InteropServices.Marshal]::ReleaseComObject($wb) | Out-Null
[System.Runtime.InteropServices.Marshal]::ReleaseComObject($app) | Out-Null
""",
    "powerpoint": """
try {
    $app = [System.Runtime.InteropServices.Marshal]::GetActiveObject('PowerPoint.Application')
} catch {
    $app = New-Object -ComObject PowerPoint.Application
}
$app.Visible = $true
$pres = $app.Presentations.Add()
$app.Activate()
[System.Runtime.InteropServices.Marshal]::ReleaseComObject($pres) | Out-Null
[System.Runtime.InteropServices.Marshal]::ReleaseComObject($app) | Out-Null
""",
}

OFFICE_TITLES = {
    "word":       "Word",
    "excel":      "Excel",
    "powerpoint": "PowerPoint",
}

OFFICE_BODY_Y = {
    "word":       0.50,
    "excel":      0.45,
    "powerpoint": 0.55,
}

OFFICE_MAP = {
    "word": "word", "winword": "word",
    "excel": "excel",
    "powerpoint": "powerpoint", "ppt": "powerpoint",
}

# Windows built-in and common apps — launched directly without COM
BUILTIN_APP_MAP = {
    "calculator": "calc.exe",
    "calc":       "calc.exe",
    "paint":      "mspaint.exe",
    "mspaint":    "mspaint.exe",
    "explorer":   "explorer.exe",
    "files":      "explorer.exe",
    "file explorer": "explorer.exe",
    "wordpad":    "wordpad.exe",
    "cmd":        "cmd.exe",
    "terminal":   "wt.exe",          # Windows Terminal
    "powershell": "powershell.exe",
    "task manager": "taskmgr.exe",
    "taskmgr":    "taskmgr.exe",
    "snipping tool": "SnippingTool.exe",
    "snip":       "SnippingTool.exe",
    "settings":   "ms-settings:",    # ms-settings URI
    "store":      "ms-windows-store:",
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def find_notepadpp() -> str | None:
    return next((p for p in NOTEPADPP_PATHS if os.path.exists(p)), None)


# FIX 5: Resolve the Chrome executable from known paths
def find_chrome() -> str | None:
    for p in CHROME_PATHS:
        if os.path.exists(p):
            return p
    return None


def activate_window_by_title(title_fragment: str):
    try:
        ps_cmd = (
            "Add-Type -AssemblyName Microsoft.VisualBasic; "
            f"[Microsoft.VisualBasic.Interaction]::AppActivate('{title_fragment}')"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, timeout=5,
        )
    except Exception as e:
        logger.debug(f"activate_window_by_title failed: {e}")


def capture_screen(max_width: int = 1280, jpeg_quality: int = 70) -> str:
    img = ImageGrab.grab()
    w, h = img.size
    if w > max_width:
        ratio = max_width / w
        img   = img.resize((max_width, int(h * ratio)), Image.LANCZOS)
    buf = BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def open_office_via_com(office_key: str):
    script = OFFICE_COM_SCRIPTS.get(office_key, "")
    if not script:
        return
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            creationflags=flags,
        )
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        logger.debug(f"COM script for {office_key} still running after 8s (normal)")
    except Exception as e:
        logger.warning(f"open_office_via_com({office_key}) error: {e}")


# ── Agent ──────────────────────────────────────────────────────────────────────

class DeviceAgent:

    def __init__(self):
        self.ws_url  = f"{CLOUD_BRAIN_URL}/ws/device/{DEVICE_ID}"
        self.running = True

    # ── Connection management ──────────────────────────────────────────────────

    async def connect(self):
        delay = 5
        while self.running:
            try:
                logger.info(f"Connecting to {self.ws_url} as '{DEVICE_ID}'")
                headers = {"X-Api-Key": API_KEY} if API_KEY else {}
                async with websockets.connect(
                    self.ws_url,
                    additional_headers=headers,
                    ping_interval=None,
                    ping_timeout=None,
                    close_timeout=10,
                ) as ws:
                    logger.info("✓ Connected to AetherAI Cloud Brain")
                    delay = 5
                    await asyncio.gather(self._listen(ws), self._keepalive(ws))

            except websockets.ConnectionClosed as e:
                logger.warning(f"Disconnected ({e.code} {e.reason}). Reconnecting in {delay}s…")
            except Exception as e:
                logger.error(f"Connection error: {e}. Retrying in {delay}s…")

            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RECONNECT_DELAY)

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
                        "type":       "screen_info",
                        "width":      w,
                        "height":     h,
                        "request_id": data.get("request_id", ""),
                    }))
                else:
                    logger.debug(f"Unknown message type: {msg_type}")

            except Exception as e:
                logger.error(f"Message handling error: {e}", exc_info=True)

    # ── Screenshot ─────────────────────────────────────────────────────────────

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
        except Exception as e:
            await ws.send(json.dumps({
                "type":       "screenshot_result",
                "request_id": request_id,
                "error":      str(e),
            }))

    # ── Action dispatch (with retry) ───────────────────────────────────────────

    async def _handle_action(self, ws, data: dict):
        action     = data.get("action", "")
        params     = data.get("parameters", {})
        request_id = data.get("request_id", "")

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

        result = await self._execute_with_retry(ws, action, params, request_id)

        if request_id and action != "screenshot_and_return":
            await ws.send(json.dumps({
                "type":       "action_result",
                "request_id": request_id,
                "result":     result,
            }))

    async def _execute_with_retry(self, ws, action: str, params: dict,
                                   request_id: str) -> str:
        last_error = ""
        for attempt in range(MAX_ACTION_RETRIES + 1):
            if attempt > 0:
                logger.info(f"Retry {attempt} for action '{action}'")
                await asyncio.sleep(0.8 * attempt)
            try:
                return await self._execute_action(ws, action, params, request_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Action '{action}' attempt {attempt} failed: {e}")

        return f"Error after {MAX_ACTION_RETRIES + 1} attempts: {last_error}"

    async def _execute_action(self, ws, action: str, params: dict,
                               request_id: str) -> str:

        if action == "click":
            x, y = int(params["x"]), int(params["y"])
            pyautogui.click(x, y)
            return f"Clicked ({x}, {y})"

        elif action == "double_click":
            x, y = int(params["x"]), int(params["y"])
            pyautogui.doubleClick(x, y)
            return f"Double-clicked ({x}, {y})"

        elif action == "right_click":
            x, y = int(params["x"]), int(params["y"])
            pyautogui.rightClick(x, y)
            return f"Right-clicked ({x}, {y})"

        elif action == "move":
            x, y = int(params["x"]), int(params["y"])
            pyautogui.moveTo(x, y, duration=0.3)
            return f"Moved to ({x}, {y})"

        elif action == "type":
            text = params.get("text", "")
            if not text:
                return "type: no text provided"

            # FIX 6: Activate target window and wait before typing so keystrokes
            # don't land in the wrong window (e.g. Notepad opened in background).
            activate_app = params.get("activate_app", "").strip()
            if activate_app:
                # Caller specified which window to focus (e.g. "Notepad")
                activate_window_by_title(activate_app)
                await asyncio.sleep(0.4)
            else:
                # General guard: small pause + re-assert focus via a neutral
                # click at the current mouse position so any recently opened
                # window has time to become the foreground window.
                await asyncio.sleep(0.5)
                try:
                    pyautogui.click(*pyautogui.position())
                except Exception:
                    pass
                await asyncio.sleep(0.2)

            # Try clipboard paste first (fast, handles all characters)
            try:
                import pyperclip
                pyperclip.copy(text)
                await asyncio.sleep(0.2)
                pyautogui.hotkey("ctrl", "v")
                await asyncio.sleep(0.3)
            except Exception:
                safe_text = text[:500]
                pyautogui.typewrite(safe_text, interval=0.03)
            return f"Typed {len(text)} chars"

        elif action == "type_special":
            text = params.get("text", "")
            try:
                import pyperclip
                pyperclip.copy(text)
                pyautogui.hotkey("ctrl", "v")
            except Exception:
                pyautogui.typewrite(text[:500], interval=0.03)
            return f"Pasted: {text[:120]}"

        elif action == "hotkey":
            keys = params.get("keys", [])
            if not keys:
                return "hotkey: no keys provided"
            pyautogui.hotkey(*keys)
            return f"Hotkey: {'+'.join(keys)}"

        elif action == "scroll":
            x      = int(params.get("x", pyautogui.size()[0] // 2))
            y      = int(params.get("y", pyautogui.size()[1] // 2))
            clicks = int(params.get("clicks", 3))
            pyautogui.scroll(clicks, x=x, y=y)
            return f"Scrolled {clicks} at ({x},{y})"

        elif action == "open_app":
            return await self._open_app(params.get("app", "").strip())

        elif action == "new_file":
            return await self._new_file(params.get("app", "notepad").strip().lower())

        elif action == "run_command":
            cmd  = params.get("command", "")
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=30
            )
            return (proc.stdout or proc.stderr or "Done").strip()[:500]

        elif action == "wait":
            ms = int(params.get("ms", 1000))
            await asyncio.sleep(ms / 1000)
            return f"Waited {ms}ms"

        elif action == "screenshot_and_return":
            img_b64 = capture_screen()
            await ws.send(json.dumps({
                "type":         "screenshot_result",
                "request_id":   request_id,
                "image_base64": img_b64,
                "timestamp":    time.time(),
            }))
            return "screenshot_sent"

        else:
            return f"Unknown action: {action}"

    # ── App launchers ──────────────────────────────────────────────────────────

    async def _open_app(self, app: str) -> str:
        app_lc = app.lower().strip()

        # Office apps → delegate to _new_file (opens new doc via COM)
        office_key = OFFICE_MAP.get(app_lc)
        if office_key:
            return await self._new_file(office_key)

        # Notepad++
        if "notepad++" in app_lc or "notepadpp" in app_lc:
            npp = find_notepadpp()
            if npp:
                subprocess.Popen([npp])
                await asyncio.sleep(2.5)
                activate_window_by_title("Notepad++")
                return "Opened Notepad++"

        # Notepad (plain)
        if app_lc == "notepad":
            return await self._new_file("notepad")

        # FIX 5: Chrome — resolve the full executable path from known locations
        if "chrome" in app_lc:
            chrome_path = find_chrome()
            if chrome_path and chrome_path.endswith(".exe"):
                subprocess.Popen([chrome_path])
                await asyncio.sleep(2.5)
                activate_window_by_title("Chrome")
                return "Opened Google Chrome"
            if sys.platform == "win32":
                subprocess.Popen('start "" "chrome"', shell=True)
                await asyncio.sleep(2.5)
                return "Opened Chrome (shell)"

        # Edge
        if "edge" in app_lc:
            if sys.platform == "win32":
                subprocess.Popen('start "" "msedge"', shell=True)
                await asyncio.sleep(2.5)
                return "Opened Microsoft Edge"

        # Firefox
        if "firefox" in app_lc:
            if sys.platform == "win32":
                subprocess.Popen('start "" "firefox"', shell=True)
                await asyncio.sleep(2.5)
                return "Opened Firefox"

        # Windows built-in apps (calculator, paint, explorer, etc.)
        for key, exe in BUILTIN_APP_MAP.items():
            if key in app_lc:
                if exe.endswith(":"):
                    # ms-uri scheme (settings, store)
                    subprocess.Popen(f'start "" "{exe}"', shell=True)
                    await asyncio.sleep(1.5)
                    return f"Opened {key}"
                if sys.platform == "win32":
                    try:
                        subprocess.Popen([exe])
                    except FileNotFoundError:
                        subprocess.Popen(f'start "" "{exe}"', shell=True)
                    await asyncio.sleep(1.5)
                    return f"Opened {key}"

        # Generic OS open fallback
        if sys.platform == "win32":
            subprocess.Popen(f'start "" "{app}"', shell=True)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-a", app])
        else:
            subprocess.Popen([app])
        await asyncio.sleep(1.5)
        return f"Opened: {app}"

    async def _new_file(self, app: str) -> str:
        app_lc = app.lower().strip()

        if app_lc == "notepad":
            subprocess.Popen(["notepad.exe"])
            await asyncio.sleep(1.5)
            activate_window_by_title("Notepad")
            await asyncio.sleep(0.4)
            sw, sh = pyautogui.size()
            pyautogui.click(sw // 2, sh // 2)
            return "Opened new Notepad window"

        if "notepad++" in app_lc or "notepadpp" in app_lc:
            npp = find_notepadpp()
            if npp:
                subprocess.Popen([npp])
                await asyncio.sleep(2.5)
                activate_window_by_title("Notepad++")
                await asyncio.sleep(0.4)
                pyautogui.hotkey("ctrl", "n")
                await asyncio.sleep(0.5)
                sw, sh = pyautogui.size()
                pyautogui.click(sw // 2, sh // 2)
                return "Opened new Notepad++ window"
            logger.warning("Notepad++ not found — falling back to Notepad")
            return await self._new_file("notepad")

        office_key = OFFICE_MAP.get(app_lc)
        if office_key:
            logger.info(f"Opening {office_key} via COM…")
            await asyncio.get_event_loop().run_in_executor(
                None, open_office_via_com, office_key
            )
            wait = 5.0 if office_key in ("word", "powerpoint") else 4.0
            await asyncio.sleep(wait)

            title_hint = OFFICE_TITLES[office_key]
            activate_window_by_title(title_hint)
            await asyncio.sleep(0.8)

            sw, sh   = pyautogui.size()
            body_y   = OFFICE_BODY_Y.get(office_key, 0.50)
            click_y  = int(sh * body_y)
            pyautogui.click(sw // 2, click_y)
            await asyncio.sleep(0.4)
            activate_window_by_title(title_hint)
            await asyncio.sleep(0.3)
            pyautogui.click(sw // 2, click_y)
            return f"Opened new {office_key} document"

        return f"new_file: unsupported app '{app}'"

    # ── Vision loop ────────────────────────────────────────────────────────────

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

                try:
                    response_raw = await asyncio.wait_for(
                        self._wait_for_vision_response(ws, request_id),
                        timeout=30.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"Vision step {step_num}: cloud response timed out")
                    await ws.send(json.dumps({
                        "type":       "vision_complete",
                        "task_id":    task_id,
                        "request_id": request_id,
                        "message":    "Vision loop timed out waiting for cloud response",
                        "steps_taken": step_num,
                    }))
                    return

                response    = json.loads(response_raw)
                action_type = response.get("action")

                if action_type == "done":
                    await ws.send(json.dumps({
                        "type":        "vision_complete",
                        "task_id":     task_id,
                        "request_id":  request_id,
                        "message":     response.get("message", "Task complete"),
                        "steps_taken": step_num,
                    }))
                    return

                result = await self._execute_with_retry(ws, action_type,
                                                         response.get("parameters", {}), "")
                logger.info(f"Vision step {step_num} ({action_type}): {result}")
                await asyncio.sleep(0.8)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Vision loop error at step {step_num}: {e}", exc_info=True)
                await ws.send(json.dumps({
                    "type":        "vision_complete",
                    "task_id":     task_id,
                    "request_id":  request_id,
                    "message":     f"Vision loop error: {e}",
                    "steps_taken": step_num,
                }))
                return

        await ws.send(json.dumps({
            "type":        "vision_complete",
            "task_id":     task_id,
            "request_id":  request_id,
            "message":     f"Vision loop reached max steps ({max_steps})",
            "steps_taken": max_steps,
        }))

    async def _wait_for_vision_response(self, ws, request_id: str) -> str:
        async for raw in ws:
            data = json.loads(raw)
            if (data.get("type") == "vision_action"
                    and data.get("request_id") == request_id):
                return raw
            elif data.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong"}))


# ── Entry point ────────────────────────────────────────────────────────────────

async def main():
    logger.info(f"AetherAI Device Agent — Device ID: {DEVICE_ID}")
    logger.info(f"Cloud: {CLOUD_BRAIN_URL}")
    logger.info("Press Ctrl+C to stop. Move mouse to top-left corner to emergency-stop.")
    agent = DeviceAgent()
    try:
        await agent.connect()
    except KeyboardInterrupt:
        logger.info("Device Agent stopped.")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
