"""
AetherAI — Device Agent  (Stage 6 — patch 7)

FIX 2  asyncio.get_event_loop() in _new_file() replaced with
       asyncio.get_running_loop(). get_event_loop() is deprecated in
       Python 3.10+ and raises RuntimeError in Python 3.12.
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

# ── Config ────────────────────────────────────────────────────────────────────

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


# ── pywinauto availability ────────────────────────────────────────────────────

def _check_pywinauto() -> bool:
    try:
        import pywinauto  # noqa
        return True
    except ImportError:
        return False

PYWINAUTO_AVAILABLE = _check_pywinauto()
logger.info(f"[DeviceAgent] pywinauto={'available' if PYWINAUTO_AVAILABLE else 'not installed'}")


# ── App paths ─────────────────────────────────────────────────────────────────

NOTEPADPP_PATHS = [
    r"C:\Program Files\Notepad++\notepad++.exe",
    r"C:\Program Files (x86)\Notepad++\notepad++.exe",
    r"C:\Users\patri\AppData\Local\Programs\Notepad++\notepad++.exe",
    r"C:\Users\patri\scoop\apps\notepadplusplus\current\notepad++.exe",
]

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Users\patri\AppData\Local\Google\Chrome\Application\chrome.exe",
    r"C:\Users\Public\Desktop\Google Chrome.lnk",
]

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

OFFICE_TITLES = {"word": "Word", "excel": "Excel", "powerpoint": "PowerPoint"}
OFFICE_BODY_Y = {"word": 0.50, "excel": 0.45, "powerpoint": 0.55}
OFFICE_MAP    = {
    "word": "word",   "winword": "word",
    "excel": "excel",
    "powerpoint": "powerpoint", "ppt": "powerpoint",
}

BUILTIN_APP_MAP = {
    "calculator":    "calc.exe",
    "calc":          "calc.exe",
    "paint":         "mspaint.exe",
    "mspaint":       "mspaint.exe",
    "explorer":      "explorer.exe",
    "files":         "explorer.exe",
    "file explorer": "explorer.exe",
    "wordpad":       "wordpad.exe",
    "cmd":           "cmd.exe",
    "terminal":      "wt.exe",
    "powershell":    "powershell.exe",
    "task manager":  "taskmgr.exe",
    "taskmgr":       "taskmgr.exe",
    "snipping tool": "SnippingTool.exe",
    "snip":          "SnippingTool.exe",
    "settings":      "ms-settings:",
    "store":         "ms-windows-store:",
    "notepad":       "notepad.exe",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def find_notepadpp() -> str | None:
    return next((p for p in NOTEPADPP_PATHS if os.path.exists(p)), None)

def find_chrome() -> str | None:
    return next((p for p in CHROME_PATHS if os.path.exists(p)), None)

def activate_window(title_fragment: str):
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
        logger.debug(f"activate_window failed: {e}")

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


# ═════════════════════════════════════════════════════════════════════════════
# DEVICE AGENT
# ═════════════════════════════════════════════════════════════════════════════

class DeviceAgent:

    def __init__(self):
        self.ws_url  = f"{CLOUD_BRAIN_URL}/ws/device/{DEVICE_ID}"
        self.running = True
        self._vision_queues: dict[str, asyncio.Queue] = {}

    # ── Connection ─────────────────────────────────────────────────────────────

    async def connect(self):
        delay = 5
        while self.running:
            try:
                url = self.ws_url
                if API_KEY:
                    url = f"{url}?api_key={API_KEY}"
                logger.info(f"Connecting to {self.ws_url} as '{DEVICE_ID}'")
                async with websockets.connect(
                    url,
                    ping_interval=None,
                    ping_timeout=None,
                    close_timeout=10,
                ) as ws:
                    logger.info("Connected to AetherAI Cloud Brain")
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

    # ── Listen loop ─────────────────────────────────────────────────────────────

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
                elif msg_type == "vision_action":
                    request_id = data.get("request_id", "")
                    q = self._vision_queues.get(request_id)
                    if q:
                        await q.put(raw)
                    else:
                        logger.debug(f"[Listen] vision_action for unknown request_id={request_id!r}")
                else:
                    logger.debug(f"Unknown message type: {msg_type}")

            except Exception as e:
                logger.error(f"Message handling error: {e}", exc_info=True)

    # ── Screenshot ──────────────────────────────────────────────────────────────

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

    # ── Action dispatch ─────────────────────────────────────────────────────────

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

    # ── Action executor ─────────────────────────────────────────────────────────

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
            activate_app = params.get("activate_app", "").strip()
            if activate_app:
                activate_window(activate_app)
                await asyncio.sleep(0.4)
            else:
                await asyncio.sleep(0.5)
                try:
                    pyautogui.click(*pyautogui.position())
                except Exception:
                    pass
                await asyncio.sleep(0.2)
            try:
                import pyperclip
                pyperclip.copy(text)
                await asyncio.sleep(0.2)
                pyautogui.hotkey("ctrl", "v")
                await asyncio.sleep(0.3)
            except Exception:
                pyautogui.typewrite(text[:500], interval=0.03)
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

        elif action == "calculator_input":
            return await self._calculator_input(params.get("expression", ""))

        elif action == "navigate_chrome":
            return await self._navigate_chrome(params.get("url", ""))

        elif action == "find_and_open_file":
            return await self._find_and_open_file(
                params.get("filename", ""),
                params.get("search_in", str(Path.home())),
            )

        elif action == "open_folder":
            return await self._open_folder(params.get("path", ""))

        elif action == "list_files":
            return await self._list_files(params.get("path", str(Path.home())))

        elif action == "click_button":
            return await self._click_button(
                params.get("window_title", ""),
                params.get("button_text", ""),
            )

        elif action == "window_type":
            return await self._window_type(
                params.get("window_title", ""),
                params.get("text", ""),
            )

        elif action == "close_window":
            return await self._close_window(params.get("title", ""))

        elif action == "focus_window":
            title = params.get("title", "")
            activate_window(title)
            await asyncio.sleep(0.5)
            return f"Focused window: {title}"

        else:
            return f"Unknown action: {action}"

    # ── App launchers ───────────────────────────────────────────────────────────

    async def _open_app(self, app: str) -> str:
        app_lc = app.lower().strip()
        office_key = OFFICE_MAP.get(app_lc)
        if office_key:
            return await self._new_file(office_key)

        if "notepad++" in app_lc or "notepadpp" in app_lc:
            npp = find_notepadpp()
            if npp:
                subprocess.Popen([npp])
                await asyncio.sleep(2.5)
                activate_window("Notepad++")
                return "Opened Notepad++"

        if app_lc == "notepad":
            return await self._new_file("notepad")

        if "chrome" in app_lc:
            chrome_path = find_chrome()
            if chrome_path and chrome_path.endswith(".exe"):
                subprocess.Popen([chrome_path])
                await asyncio.sleep(2.5)
                activate_window("Chrome")
                return "Opened Google Chrome"
            if sys.platform == "win32":
                subprocess.Popen('start "" "chrome"', shell=True)
                await asyncio.sleep(2.5)
                return "Opened Chrome (shell)"

        if "edge" in app_lc:
            if sys.platform == "win32":
                subprocess.Popen('start "" "msedge"', shell=True)
                await asyncio.sleep(2.5)
                return "Opened Microsoft Edge"

        if "firefox" in app_lc:
            if sys.platform == "win32":
                subprocess.Popen('start "" "firefox"', shell=True)
                await asyncio.sleep(2.5)
                return "Opened Firefox"

        for key, exe in BUILTIN_APP_MAP.items():
            if key in app_lc:
                if exe.endswith(":"):
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
            activate_window("Notepad")
            await asyncio.sleep(0.4)
            sw, sh = pyautogui.size()
            pyautogui.click(sw // 2, sh // 2)
            return "Opened new Notepad window"

        if "notepad++" in app_lc or "notepadpp" in app_lc:
            npp = find_notepadpp()
            if npp:
                subprocess.Popen([npp])
                await asyncio.sleep(2.5)
                activate_window("Notepad++")
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
            # FIX 2: use get_running_loop() — get_event_loop() is deprecated
            # in Python 3.10+ and raises RuntimeError in Python 3.12
            await asyncio.get_running_loop().run_in_executor(
                None, open_office_via_com, office_key
            )
            wait = 5.0 if office_key in ("word", "powerpoint") else 4.0
            await asyncio.sleep(wait)
            title_hint = OFFICE_TITLES[office_key]
            activate_window(title_hint)
            await asyncio.sleep(0.8)
            sw, sh  = pyautogui.size()
            body_y  = OFFICE_BODY_Y.get(office_key, 0.50)
            click_y = int(sh * body_y)
            pyautogui.click(sw // 2, click_y)
            await asyncio.sleep(0.4)
            activate_window(title_hint)
            await asyncio.sleep(0.3)
            pyautogui.click(sw // 2, click_y)
            return f"Opened new {office_key} document"

        return f"new_file: unsupported app '{app}'"

    # ── Chrome navigation ──────────────────────────────────────────────────────

    async def _navigate_chrome(self, url: str) -> str:
        if not url:
            return "navigate_chrome: no URL provided"

        activate_window("Chrome")
        await asyncio.sleep(0.5)

        pyautogui.hotkey("ctrl", "l")
        await asyncio.sleep(0.4)

        pyautogui.hotkey("ctrl", "a")
        await asyncio.sleep(0.1)

        try:
            import pyperclip
            pyperclip.copy(url)
            await asyncio.sleep(0.2)
            pyautogui.hotkey("ctrl", "v")
        except Exception:
            pyautogui.typewrite(url, interval=0.03)

        await asyncio.sleep(0.3)
        pyautogui.press("enter")
        await asyncio.sleep(1.0)
        return f"Navigated Chrome to: {url}"

    # ── Calculator input ───────────────────────────────────────────────────────

    async def _calculator_input(self, expression: str) -> str:
        if not expression:
            return "calculator_input: no expression provided"

        activate_window("Calculator")
        await asyncio.sleep(0.5)

        sw, sh = pyautogui.size()
        pyautogui.click(sw // 2, sh // 2)
        await asyncio.sleep(0.3)

        pyautogui.press("escape")
        await asyncio.sleep(0.1)

        key_map = {
            "*": ["*"], "/": ["/"], "+": ["+"], "-": ["-"],
            "=": ["enter"], "(": ["("], ")": [")"], ".": ["."],
            " ": [],
        }

        for char in expression.strip():
            if char.isdigit():
                pyautogui.press(char)
            elif char in key_map:
                for k in key_map[char]:
                    pyautogui.press(k)
            await asyncio.sleep(0.05)

        await asyncio.sleep(0.1)
        pyautogui.press("enter")
        await asyncio.sleep(0.3)

        return f"Entered expression into Calculator: {expression}"

    # ── File/folder actions ────────────────────────────────────────────────────

    async def _find_and_open_file(self, filename: str, search_in: str) -> str:
        if not filename:
            return "find_and_open_file: no filename provided"
        search_path = Path(search_in)
        matches = []
        for match in search_path.rglob(filename):
            if match.is_file():
                matches.append(str(match))
                if len(matches) >= 5: break
        if not matches:
            for match in search_path.rglob(f"*{filename}*"):
                if match.is_file():
                    matches.append(str(match))
                    if len(matches) >= 5: break
        if not matches:
            return f"⚠️ File '{filename}' not found in {search_in}"
        target = matches[0]
        os.startfile(target)
        await asyncio.sleep(1.5)
        result = f"✅ Opened: {target}"
        if len(matches) > 1:
            result += f"\n(Also found: {', '.join(matches[1:])})"
        return result

    async def _open_folder(self, path: str) -> str:
        if not path:
            path = str(Path.home())
        folder = Path(path)
        known = {
            "documents": Path.home() / "Documents",
            "downloads": Path.home() / "Downloads",
            "desktop":   Path.home() / "Desktop",
            "pictures":  Path.home() / "Pictures",
            "music":     Path.home() / "Music",
            "videos":    Path.home() / "Videos",
            "home":      Path.home(),
        }
        if not folder.exists():
            folder = known.get(path.lower(), Path.home())
        subprocess.Popen(["explorer.exe", str(folder)])
        await asyncio.sleep(1.5)
        return f"✅ Opened folder: {folder}"

    async def _list_files(self, path: str) -> str:
        folder = Path(path)
        known = {
            "documents": Path.home() / "Documents",
            "downloads": Path.home() / "Downloads",
            "desktop":   Path.home() / "Desktop",
            "pictures":  Path.home() / "Pictures",
            "home":      Path.home(),
        }
        if not folder.exists():
            folder = known.get(path.lower(), Path.home())
        try:
            items = list(folder.iterdir())
            files = sorted([f.name for f in items if f.is_file()])[:20]
            dirs  = sorted([f.name + "/" for f in items if f.is_dir()])[:10]
            result = f"📁 **{folder}**\n\n"
            if dirs:
                result += "**Folders:**\n" + "\n".join(f"  📂 {d}" for d in dirs) + "\n\n"
            if files:
                result += "**Files:**\n" + "\n".join(f"  📄 {f}" for f in files)
            if not files and not dirs:
                result += "_(empty folder)_"
            return result
        except PermissionError:
            return f"⚠️ Permission denied: {folder}"
        except Exception as e:
            return f"⚠️ list_files failed: {e}"

    async def _click_button(self, window_title: str, button_text: str) -> str:
        if not PYWINAUTO_AVAILABLE:
            return "⚠️ pywinauto not installed. Run: pip install pywinauto"
        try:
            from pywinauto import Desktop
            windows = Desktop(backend="uia").windows(title_re=f".*{window_title}.*")
            if not windows:
                return f"⚠️ Window '{window_title}' not found"
            win = windows[0]
            win.set_focus()
            await asyncio.sleep(0.3)
            btn = win.child_window(title=button_text, control_type="Button")
            if btn.exists():
                btn.click_input()
                return f"✅ Clicked '{button_text}'"
            for ctrl in win.descendants(control_type="Button"):
                if button_text.lower() in ctrl.window_text().lower():
                    ctrl.click_input()
                    return f"✅ Clicked '{ctrl.window_text()}'"
            return f"⚠️ Button '{button_text}' not found in '{window_title}'"
        except Exception as e:
            return f"⚠️ click_button failed: {e}"

    async def _window_type(self, window_title: str, text: str) -> str:
        activate_window(window_title)
        await asyncio.sleep(0.5)
        try:
            import pyperclip
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "v")
            return f"✅ Typed {len(text)} chars into '{window_title}'"
        except Exception as e:
            return f"⚠️ window_type failed: {e}"

    async def _close_window(self, title: str) -> str:
        if PYWINAUTO_AVAILABLE:
            try:
                from pywinauto import Desktop
                windows = Desktop(backend="uia").windows(title_re=f".*{title}.*")
                if not windows:
                    return f"⚠️ No window matching '{title}'"
                count = 0
                for win in windows:
                    try:
                        win.close()
                        count += 1
                    except Exception:
                        pass
                return f"✅ Closed {count} window(s) matching '{title}'"
            except Exception as e:
                return f"⚠️ close_window failed: {e}"
        else:
            activate_window(title)
            await asyncio.sleep(0.4)
            pyautogui.hotkey("alt", "F4")
            return f"✅ Sent Alt+F4 to '{title}'"

    # ── Vision loop ─────────────────────────────────────────────────────────────

    async def _vision_loop(self, ws, data: dict):
        goal       = data.get("goal", "")
        task_id    = data.get("task_id", "")
        max_steps  = data.get("max_steps", 10)
        request_id = data.get("request_id", "")

        logger.info(f"Vision loop started. Goal: {goal}")
        q: asyncio.Queue[str] = asyncio.Queue()
        self._vision_queues[request_id] = q

        try:
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
                        response_raw = await asyncio.wait_for(q.get(), timeout=30.0)
                    except asyncio.TimeoutError:
                        logger.warning(f"Vision step {step_num}: cloud response timed out")
                        await ws.send(json.dumps({
                            "type":        "vision_complete",
                            "task_id":     task_id,
                            "request_id":  request_id,
                            "message":     "Vision loop timed out waiting for cloud response",
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

                    result = await self._execute_with_retry(
                        ws, action_type, response.get("parameters", {}), ""
                    )
                    logger.info(f"Vision step {step_num} ({action_type}): {result}")
                    await asyncio.sleep(0.8)

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Vision loop error step {step_num}: {e}", exc_info=True)
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
                "message":     f"Reached max steps ({max_steps})",
                "steps_taken": max_steps,
            }))

        finally:
            self._vision_queues.pop(request_id, None)


# ── Entry point ──────────────────────────────────────────────────────────────

async def main():
    logger.info(f"AetherAI Device Agent — Device ID: {DEVICE_ID}")
    logger.info(f"Cloud: {CLOUD_BRAIN_URL}")
    logger.info(f"pywinauto: {'available' if PYWINAUTO_AVAILABLE else 'not installed'}")
    logger.info("Press Ctrl+C to stop. Move mouse to top-left to emergency-stop.")
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
