"""
AetherAI Cloud Brain — Stage 7.1 (Bronny Control Panel)

Stage 7.1 additions
────────────────────
BRONNY 3  POST /bronny/control  — queue a command for Bronny (volume, brightness,
          LED, sleep, restart, party, play, pause, resume, next, stop, seek).
          Commands are delivered to the device on its next /bronny/heartbeat call.
BRONNY 4  GET  /youtube/search  — search YouTube via yt-dlp, returns video list.
          Falls back gracefully if yt-dlp is unavailable (UI uses Invidious then).
BRONNY 5  GET  /bronny/media    — stream audio/video from a YouTube URL to the
          caller (ESP32 or browser).  mode=audio → MP3, mode=video → best ≤480p.
UPDATED   POST /bronny/heartbeat — response now includes a `commands` list that
          the ESP32 must parse and execute, then clear.

Stage 7 retained
─────────────────
BRONNY 1  POST /bronny/heartbeat — device keepalive + task-list entry on connect
BRONNY 2  GET  /bronny/status   — online/offline badge poll
VOICE 2   GET  /tts/voices      — list edge-tts voices
VOICE 3   POST /voice/text      — pre-transcribed text → LLM → TTS → MP3
SSE 1     POST /stream          — SSE streaming for chat commands
All Stage 5/6 fixes retained.
"""

from contextlib import asynccontextmanager
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

import asyncio
import json
import os
import secrets
import shutil
import uuid
from collections import deque
from datetime import datetime
from typing import Optional, AsyncGenerator

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from orchestrator import Orchestrator
from memory import MemoryManager
from utils.websocket_manager import WebSocketManager
from config import settings

# ── Global instances ──────────────────────────────────────────────────────────

ws_manager   = WebSocketManager()
memory       = MemoryManager()
orchestrator = Orchestrator(memory=memory, ws_manager=ws_manager)

_UI_SESSION_TOKEN   = secrets.token_urlsafe(24)
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "5"))

# ── Bronny device status (updated by /bronny/heartbeat) ──────────────────────
_bronny_status: dict = {"online": False, "last_seen": None, "version": None, "device": None}

# ── Bronny pending command queue (flushed on next heartbeat) ─────────────────
_bronny_cmd_queue: deque = deque(maxlen=10)

# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await ws_manager.start()
    yield

# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(
    title="AetherAI Cloud Brain",
    description="Personal AI Agent System — Stage 7.1",
    version="7.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ── API key middleware ────────────────────────────────────────────────────────

_PUBLIC_PREFIXES = (
    "/ui", "/health", "/docs", "/openapi", "/redoc",
    "/files/download", "/bronny", "/video", "/youtube",
)
_PUBLIC_EXACT = frozenset(["/", "/health", "/ui/config"])

class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not settings.API_KEY:
            return await call_next(request)
        path = request.url.path
        if path in _PUBLIC_EXACT or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)
        if request.headers.get("X-Api-Key", "") != settings.API_KEY:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing API key. Set X-Api-Key header."},
            )
        return await call_next(request)

app.add_middleware(ApiKeyMiddleware)

from video_routes import router as video_router
app.include_router(video_router)

# ── Models ────────────────────────────────────────────────────────────────────

class CommandRequest(BaseModel):
    command:    str
    source:     str = "web"
    session_id: Optional[str] = None

class CommandResponse(BaseModel):
    task_id: str
    status:  str
    message: str
    plan:    Optional[list] = None

class StatusResponse(BaseModel):
    task_id:    str
    command:    str
    status:     str
    steps:      list
    result:     Optional[str] = None
    created_at: str
    updated_at: str

class PrefRequest(BaseModel):
    label: str
    value: str

class VoiceTextRequest(BaseModel):
    text: str

class BronnyHeartbeatRequest(BaseModel):
    device:  str = "bronny"
    version: str = "2.0"

class BronnyControlRequest(BaseModel):
    command:    str
    value:      Optional[int]  = None   # volume 0-100, brightness 0-100, seek seconds
    mode:       Optional[str]  = None   # led mode string, or media mode "audio"/"video"
    color:      Optional[str]  = None   # hex e.g. "#ff3ca0"
    speed:      Optional[int]  = None   # 1-10
    active:     Optional[bool] = None   # party on/off
    visualizer: Optional[str]  = None   # "bars","wave","spectrum","circle",…
    led_mode:   Optional[str]  = None   # party LED mode
    url:        Optional[str]  = None   # YouTube URL for play command
    rainbow:    Optional[bool] = None   # rainbow LED during playback
    title:      Optional[str]  = None
    duration:   Optional[int]  = None   # seconds
    position:   Optional[int]  = None   # seek position in seconds

# ── Core endpoints ────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"system": "AetherAI Cloud Brain", "status": "online", "version": "7.1.0"}

@app.get("/health")
async def health():
    return {
        "status":            "healthy",
        "timestamp":         datetime.utcnow().isoformat(),
        "devices_connected": ws_manager.device_count(),
        "task_stats":        memory.get_task_stats(),
    }

@app.get("/ui/config")
async def ui_config():
    return {"api_key": settings.API_KEY, "session_token": _UI_SESSION_TOKEN}

@app.post("/command", response_model=CommandResponse)
async def receive_command(req: CommandRequest):
    stats = memory.get_task_stats()
    if (stats.get("running", 0) + stats.get("planning", 0)) >= MAX_CONCURRENT_TASKS:
        raise HTTPException(
            status_code=429,
            detail="Too many tasks in progress. Wait for one to finish.",
        )
    task_id    = str(uuid.uuid4())
    session_id = req.session_id or ""
    memory.create_task(task_id, req.command, req.source)
    asyncio.create_task(orchestrator.run_task(task_id, req.command, session_id))
    return CommandResponse(
        task_id=task_id, status="started",
        message=f"Task received: '{req.command}'",
    )

# ── SSE streaming endpoint ────────────────────────────────────────────────────

def _load_user_ctx() -> str:
    try:
        from agents.memory_agent import MemoryAgent
        return MemoryAgent.load_context(memory)
    except Exception:
        return ""

@app.post("/stream")
async def stream_command(req: CommandRequest):
    """SSE endpoint — streams chat tokens or returns task_id for task commands."""
    async def generate() -> AsyncGenerator[str, None]:
        user_ctx     = _load_user_ctx()
        command_type = await orchestrator.qwen.classify_command(
            req.command, user_context=user_ctx
        )
        if command_type == "chat":
            yield f"data: {json.dumps({'type': 'stream_start'})}\n\n"
            full = ""
            try:
                async for chunk in orchestrator.qwen.stream_answer(
                    req.command, user_context=user_ctx
                ):
                    full += chunk
                    yield f"data: {json.dumps({'type': 'chunk', 'text': chunk})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
            yield f"data: {json.dumps({'type': 'stream_end', 'full_text': full})}\n\n"
        else:
            task_id    = str(uuid.uuid4())
            session_id = req.session_id or ""
            memory.create_task(task_id, req.command, req.source)
            asyncio.create_task(orchestrator.run_task(task_id, req.command, session_id))
            yield f"data: {json.dumps({'type': 'task_created', 'task_id': task_id})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ── Voice endpoints ───────────────────────────────────────────────────────────

@app.post("/voice/text")
async def voice_text_chat(req: VoiceTextRequest):
    """
    VOICE 3 — ESP32 sends pre-transcribed text (Deepgram streaming ASR on-device).
    Skips STT entirely. Runs: text → Qwen LLM → edge-tts → MP3 bytes.

    Special trigger "bootup_intro": returns a Bronny self-introduction directly,
    skipping classify/plan. Sent by firmware doBootIntro() on first power-on.
    """
    from urllib.parse import quote as urlquote
    from agents.voice_agent import _voice_summarize, _safe_synthesize

    transcript = req.text.strip()
    if not transcript:
        raise HTTPException(status_code=400, detail="text field is empty")

    task_id = str(uuid.uuid4())
    memory.create_task(task_id, f"[Voice] {transcript[:80]}", "voice")
    memory.update_task_status(task_id, "running")
    await ws_manager.broadcast_task_update(task_id, {
        "status":  "running",
        "message": f"🎙️ Voice: {transcript[:60]}",
    })

    try:
        user_context = ""
        try:
            from agents.memory_agent import MemoryAgent
            user_context = MemoryAgent.load_context(memory)
        except Exception:
            pass

        BOOTUP_INTRO_TRIGGERS = {"bootup_intro", "bootup intro"}
        if transcript.lower().strip() in BOOTUP_INTRO_TRIGGERS:
            response_text = await orchestrator.qwen.bronny_answer(
                "Please introduce yourself in 2-3 warm, friendly sentences. "
                "Your name is Bronny. You are a voice assistant built into a "
                "physical desktop device by Patrick. You are ready and listening.",
                user_context=user_context,
            )
        else:
            command_type = await orchestrator.qwen.classify_command(
                transcript, user_context=user_context
            )
            if command_type == "chat":
                response_text = await orchestrator.qwen.bronny_answer(
                    transcript, user_context=user_context
                )
            else:
                agent_task_id = str(uuid.uuid4())
                memory.create_task(agent_task_id, transcript, "voice")
                asyncio.create_task(
                    orchestrator.run_task(agent_task_id, transcript, "")
                )
                response_text = "I'm working on that for you now."

        if not response_text:
            response_text = "I wasn't able to get an answer for that. Please try again."

        spoken_text = await _voice_summarize(orchestrator.qwen, response_text, transcript)
        mp3_bytes   = await _safe_synthesize(spoken_text)

    except Exception as e:
        memory.update_task_status(task_id, "failed", result=str(e))
        await ws_manager.broadcast_task_update(task_id, {
            "status":  "failed",
            "message": f"Voice text error: {e}",
        })
        raise HTTPException(status_code=500, detail=f"Voice text error: {e}")

    if not mp3_bytes:
        memory.update_task_status(task_id, "failed", result="TTS produced no audio")
        raise HTTPException(status_code=502, detail="TTS produced no audio")

    voice_summary = f"Heard: {transcript}\n\nResponse: {spoken_text}"
    memory.update_task_status(task_id, "completed", result=voice_summary)
    await ws_manager.broadcast_task_update(task_id, {
        "status":      "completed",
        "message":     f"🎙️ {transcript}",
        "step_status": "completed",
        "output":      voice_summary,
        "result":      voice_summary,
    })

    return Response(
        content=mp3_bytes,
        media_type="audio/mpeg",
        headers={
            "X-Response-Text": urlquote(spoken_text[:300]),
            "Cache-Control":   "no-cache",
        },
    )

@app.get("/tts/voices")
async def tts_voices():
    """VOICE 2 — List all available edge-tts voices."""
    from utils.tts_client import list_voices
    voices = await list_voices()
    return {
        "current_voice": settings.TTS_VOICE,
        "voices":        voices,
    }

# ── Bronny device endpoints ───────────────────────────────────────────────────

@app.post("/bronny/heartbeat")
async def bronny_heartbeat(req: BronnyHeartbeatRequest):
    """
    BRONNY 1 — Called by the ESP32 on boot and every 30s.
    On offline → online transition, creates a task entry in the command center.
    Response now includes a `commands` array that the ESP32 must execute then clear.
    """
    was_offline = not _bronny_status["online"]

    _bronny_status["online"]    = True
    _bronny_status["last_seen"] = datetime.utcnow().isoformat()
    _bronny_status["version"]   = req.version
    _bronny_status["device"]    = req.device

    await ws_manager.broadcast_ui_event({
        "type":    "bronny_status",
        "online":  True,
        "version": req.version,
    })

    if was_offline:
        conn_task_id = str(uuid.uuid4())
        label = f"[Bronny] Device connected — v{req.version}"
        memory.create_task(conn_task_id, label, "device")
        memory.update_task_status(
            conn_task_id, "completed",
            result=f"Bronny v{req.version} online at {_bronny_status['last_seen']}",
        )
        await ws_manager.broadcast_task_update(conn_task_id, {
            "status":      "completed",
            "message":     f"🤖 {label}",
            "step_status": "completed",
            "output":      f"Bronny v{req.version} is now online.",
            "result":      f"Bronny v{req.version} is now online.",
        })

    # Flush pending commands — ESP32 reads this list and executes each one
    commands = list(_bronny_cmd_queue)
    _bronny_cmd_queue.clear()

    return {
        "ok":       True,
        "device":   req.device,
        "commands": commands,
    }


@app.get("/bronny/status")
async def bronny_status():
    """
    BRONNY 2 — Returns Bronny online/offline status.
    Bronny is considered offline if last heartbeat is > 60s ago.
    """
    if _bronny_status["last_seen"]:
        last = datetime.fromisoformat(_bronny_status["last_seen"])
        age  = (datetime.utcnow() - last).total_seconds()
        if age > 60:
            _bronny_status["online"] = False
    return _bronny_status


@app.post("/bronny/control")
async def bronny_control(req: BronnyControlRequest):
    """
    BRONNY 3 — Queue a control command for Bronny.
    Delivered to the device on its next /bronny/heartbeat call (within 30s).

    Supported commands:
      volume      {value: 0-100}
      brightness  {value: 0-100}
      sleep       {}
      restart     {}
      party       {active: bool, visualizer: str, led_mode: str, speed: 1-10, color: "#hex"}
      led         {mode: str, color: "#hex", speed: 1-10}
      play        {url: str, mode: "audio"|"video", rainbow: bool, title: str, duration: int}
      pause       {}
      resume      {}
      next        {}
      stop        {}
      seek        {position: int}  (seconds; best-effort — stream restarts from 0)
    """
    if not _bronny_status["online"]:
        raise HTTPException(status_code=503, detail="Bronny is offline")

    cmd = req.dict(exclude_none=True)
    _bronny_cmd_queue.append(cmd)

    # Notify UI that the command was queued
    await ws_manager.broadcast_ui_event({
        "type":    "bronny_command_queued",
        "command": cmd["command"],
    })

    return {"ok": True, "queued": True, "queue_depth": len(_bronny_cmd_queue)}


@app.get("/youtube/search")
async def youtube_search(q: str, limit: int = 12):
    """
    BRONNY 4 — Search YouTube using yt-dlp's ytsearch.
    Returns {videos: [{videoId, title, channel, duration, thumb}], count: N}.
    Returns 501 if yt-dlp is not installed (UI will fall back to Invidious API).
    """
    if not shutil.which("yt-dlp"):
        raise HTTPException(status_code=501, detail="yt-dlp not installed on server")

    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp",
            f"ytsearch{limit}:{q}",
            "--flat-playlist",
            "--print",
            '{"videoId":"%(id)s","title":"%(title)s","channel":"%(uploader)s",'
            '"duration":%(duration)s,"thumb":"https://img.youtube.com/vi/%(id)s/mqdefault.jpg"}',
            "--no-warnings",
            "--quiet",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        videos = []
        for line in stdout.decode(errors="replace").strip().splitlines():
            try:
                v = json.loads(line)
                if v.get("videoId"):
                    videos.append(v)
            except Exception:
                pass
        return {"videos": videos, "count": len(videos)}

    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="YouTube search timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/bronny/media")
async def bronny_media(url: str, mode: str = "audio"):
    """
    BRONNY 5 — Download and stream a YouTube URL as audio or video.

    mode=audio  → yt-dlp extracts best audio, re-encodes to MP3 (128 kbps).
                  ESP32 feeds the MP3 stream through CodecMP3Helix → ES8311.
    mode=video  → best video ≤ 480p (for browser preview; ESP32 won't call this).

    The ESP32 calls this URL exactly like it calls /voice/text response audio —
    reads the HTTP stream and pipes it through the existing MP3 decoder pipeline.
    """
    if not shutil.which("yt-dlp"):
        raise HTTPException(status_code=501, detail="yt-dlp not installed on server")

    if mode == "audio":
        args = [
            "yt-dlp", url,
            "-f", "bestaudio[ext=mp3]/bestaudio/best",
            "-x", "--audio-format", "mp3",
            "--audio-quality", "5",   # ~128 kbps — good balance for ESP32 decoder
            "-o", "-",
            "--no-playlist", "--quiet", "--no-warnings",
        ]
        media_type = "audio/mpeg"
    else:
        args = [
            "yt-dlp", url,
            "-f", "best[height<=480]/best",
            "-o", "-",
            "--no-playlist", "--quiet", "--no-warnings",
        ]
        media_type = "video/mp4"

    async def stream_media():
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            while True:
                chunk = await proc.stdout.read(8192)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                proc.kill()
            except Exception:
                pass

    return StreamingResponse(
        stream_media(),
        media_type=media_type,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Task endpoints ────────────────────────────────────────────────────────────

@app.get("/task/{task_id}", response_model=StatusResponse)
async def get_task_status(task_id: str):
    task = memory.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task

@app.get("/tasks")
async def list_tasks(limit: int = 20):
    return memory.list_tasks(limit=limit)

@app.post("/task/{task_id}/cancel")
async def cancel_task(task_id: str):
    if not orchestrator.cancel_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found or already finished")
    memory.update_task_status(task_id, "cancelled")
    return {"task_id": task_id, "status": "cancelled"}

@app.delete("/task/{task_id}")
async def delete_task(task_id: str):
    if not memory.delete_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task_id": task_id, "deleted": True}

@app.delete("/tasks/all")
async def delete_all_tasks():
    return {"deleted_count": memory.delete_all_tasks()}

# ── File endpoints ────────────────────────────────────────────────────────────

@app.get("/files")
async def list_files():
    output_dir = Path(__file__).parent.parent / "output"
    output_dir.mkdir(exist_ok=True)
    files = []
    for f in sorted(output_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file():
            files.append({
                "name":    f.name,
                "size_kb": round(f.stat().st_size / 1024, 1),
                "created": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "url":     f"/files/download/{f.name}",
            })
    return {"files": files}

@app.get("/files/download/{filename}")
async def download_file(filename: str):
    import re as _re
    if not _re.match(r'^[\w\-. ]+$', filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = Path(__file__).parent.parent / "output" / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, filename=filename)

@app.delete("/files/{filename}")
async def delete_file(filename: str):
    import re as _re
    if not _re.match(r'^[\w\-. ]+$', filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = Path(__file__).parent.parent / "output" / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    path.unlink()
    return {"filename": filename, "deleted": True}

@app.delete("/files/all/clear")
async def clear_all_files():
    output_dir = Path(__file__).parent.parent / "output"
    count = 0
    if output_dir.exists():
        for f in output_dir.iterdir():
            if f.is_file():
                f.unlink()
                count += 1
    return {"deleted_count": count}

# ── Preferences endpoints ─────────────────────────────────────────────────────

@app.get("/preferences")
async def list_preferences():
    return memory.list_preferences()

@app.post("/preferences")
async def save_preference(req: PrefRequest):
    memory.save_preference(req.label, req.value)
    return {"label": req.label, "value": req.value, "saved": True}

@app.delete("/preferences/all")
async def clear_all_preferences():
    return {"deleted_count": memory.clear_all_preferences()}

@app.delete("/preferences/{key}")
async def delete_preference(key: str):
    deleted = memory.delete_preference(key)
    if not deleted:
        raise HTTPException(status_code=404, detail="Preference not found")
    return {"key": key, "deleted": True}

# ── Devices endpoint ──────────────────────────────────────────────────────────

@app.get("/devices")
async def list_devices():
    return {"devices": ws_manager.list_devices()}

# ── WebSocket endpoints ───────────────────────────────────────────────────────

@app.websocket("/ws/device/{device_id}")
async def device_ws(websocket: WebSocket, device_id: str):
    await ws_manager.connect_device(websocket, device_id)
    try:
        while True:
            data = await websocket.receive_text()
            await ws_manager.handle_device_message(device_id, data)
    except WebSocketDisconnect:
        ws_manager.disconnect_device(device_id)

@app.websocket("/ws/ui/{session_id}")
async def ui_ws(websocket: WebSocket, session_id: str, token: Optional[str] = None):
    if _UI_SESSION_TOKEN and token != _UI_SESSION_TOKEN:
        await websocket.close(code=4401)
        return
    await ws_manager.connect_ui(websocket, session_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect_ui(session_id)

# ── Static UI files ───────────────────────────────────────────────────────────

app.mount(
    "/ui",
    StaticFiles(directory=Path(__file__).parent.parent / "web_ui", html=True),
    name="ui",
)
