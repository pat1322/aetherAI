"""
AetherAI Cloud Brain — Stage 7.1 (Bronny Control Panel)

Stage 7.1 additions
────────────────────
BRONNY 3  POST /bronny/control  — queue a command for Bronny; delivered on next heartbeat
BRONNY 4  GET  /youtube/search  — YouTube search via yt-dlp (501 if not installed)
BRONNY 5  GET  /bronny/media    — stream audio/video from YouTube URL to ESP32
UPDATED   POST /bronny/heartbeat — response now includes `commands` list for the ESP32
UPDATED   /youtube added to _PUBLIC_PREFIXES
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

_UI_SESSION_TOKEN    = secrets.token_urlsafe(24)
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "5"))

# ── Bronny device status ──────────────────────────────────────────────────────
_bronny_status: dict = {"online": False, "last_seen": None, "version": None}

# ── Bronny pending command queue (flushed on next heartbeat) ──────────────────
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

_PUBLIC_PREFIXES = ("/ui", "/health", "/docs", "/openapi", "/redoc",
                    "/files/download", "/bronny", "/video", "/youtube")
_PUBLIC_EXACT    = frozenset(["/", "/health", "/ui/config"])

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
            return JSONResponse(status_code=401,
                                content={"detail": "Invalid or missing API key. Set X-Api-Key header."})
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
    value:      Optional[int]  = None
    mode:       Optional[str]  = None
    color:      Optional[str]  = None
    speed:      Optional[int]  = None
    active:     Optional[bool] = None
    visualizer: Optional[str]  = None
    led_mode:   Optional[str]  = None
    url:        Optional[str]  = None
    rainbow:    Optional[bool] = None
    title:      Optional[str]  = None
    duration:   Optional[int]  = None
    position:   Optional[int]  = None

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
        raise HTTPException(status_code=429,
                            detail="Too many tasks in progress. Wait for one to finish.")
    task_id    = str(uuid.uuid4())
    session_id = req.session_id or ""
    memory.create_task(task_id, req.command, req.source)
    asyncio.create_task(orchestrator.run_task(task_id, req.command, session_id))
    return CommandResponse(task_id=task_id, status="started",
                           message=f"Task received: '{req.command}'")

# ── SSE streaming endpoint ────────────────────────────────────────────────────

def _load_user_ctx() -> str:
    try:
        from agents.memory_agent import MemoryAgent
        return MemoryAgent.load_context(memory)
    except Exception:
        return ""

@app.post("/stream")
async def stream_command(req: CommandRequest):
    async def generate() -> AsyncGenerator[str, None]:
        user_ctx     = _load_user_ctx()
        command_type = await orchestrator.qwen.classify_command(req.command, user_context=user_ctx)
        if command_type == "chat":
            yield f"data: {json.dumps({'type': 'stream_start'})}\n\n"
            full = ""
            try:
                async for chunk in orchestrator.qwen.stream_answer(req.command, user_context=user_ctx):
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

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ── Voice endpoints ───────────────────────────────────────────────────────────

@app.post("/voice/text")
async def voice_text_chat(req: VoiceTextRequest):
    """
    VOICE 3 - ESP32 sends pre-transcribed text, returns TTS MP3.
    Always uses bronny_answer() so Bronny never identifies as AetherAI.
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
        "message": f"[Voice] {transcript[:60]}",
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
            response_text = await orchestrator.qwen.bronny_answer(
                transcript, user_context=user_context
            )

        if not response_text:
            response_text = "I wasn't able to get an answer for that. Please try again."

        spoken_text = await _voice_summarize(orchestrator.qwen, response_text, transcript)
        mp3_bytes   = await _safe_synthesize(spoken_text)

    except Exception as e:
        memory.update_task_status(task_id, "failed", result=str(e))
        await ws_manager.broadcast_task_update(task_id, {"status": "failed",
                                                          "message": f"Voice text error: {e}"})
        raise HTTPException(status_code=500, detail=f"Voice text error: {e}")

    if not mp3_bytes:
        memory.update_task_status(task_id, "failed", result="TTS produced no audio")
        raise HTTPException(status_code=502, detail="TTS produced no audio")

    voice_summary = f"Heard: {transcript}\n\nResponse: {spoken_text}"
    memory.update_task_status(task_id, "completed", result=voice_summary)
    await ws_manager.broadcast_task_update(task_id, {
        "status": "completed", "message": f"[Voice] {transcript}",
        "step_status": "completed", "output": voice_summary, "result": voice_summary,
    })
    return Response(content=mp3_bytes, media_type="audio/mpeg",
                    headers={"X-Response-Text": urlquote(spoken_text[:300]),
                             "Cache-Control": "no-cache"})

@app.get("/tts/voices")
async def tts_voices():
    from utils.tts_client import list_voices
    voices = await list_voices()
    return {"current_voice": settings.TTS_VOICE, "voices": voices}

# ── Bronny device endpoints ───────────────────────────────────────────────────

@app.post("/bronny/heartbeat")
async def bronny_heartbeat(req: BronnyHeartbeatRequest):
    was_offline = not _bronny_status["online"]
    _bronny_status["online"]    = True
    _bronny_status["last_seen"] = datetime.utcnow().isoformat()
    _bronny_status["version"]   = req.version
    _bronny_status["device"]    = req.device

    await ws_manager.broadcast_ui_event({
        "type": "bronny_status", "online": True, "version": req.version,
    })

    if was_offline:
        conn_task_id = str(uuid.uuid4())
        label = f"[Bronny] Device connected — v{req.version}"
        memory.create_task(conn_task_id, label, "device")
        memory.update_task_status(conn_task_id, "completed",
                                  result=f"Bronny v{req.version} online")
        await ws_manager.broadcast_task_update(conn_task_id, {
            "status": "completed", "message": f"[Bronny] {label}",
            "step_status": "completed",
            "output": f"Bronny v{req.version} is now online.",
            "result": f"Bronny v{req.version} is now online.",
        })

    commands = list(_bronny_cmd_queue)
    _bronny_cmd_queue.clear()
    return {"ok": True, "device": req.device, "commands": commands}


@app.get("/bronny/status")
async def bronny_status():
    if _bronny_status["last_seen"]:
        last = datetime.fromisoformat(_bronny_status["last_seen"])
        if (datetime.utcnow() - last).total_seconds() > 60:
            _bronny_status["online"] = False
    return _bronny_status


@app.post("/bronny/control")
async def bronny_control(req: BronnyControlRequest):
    if not _bronny_status["online"]:
        raise HTTPException(status_code=503, detail="Bronny is offline")
    cmd = req.dict(exclude_none=True)
    _bronny_cmd_queue.append(cmd)
    await ws_manager.broadcast_ui_event({"type": "bronny_command_queued",
                                          "command": cmd["command"]})
    return {"ok": True, "queued": True, "queue_depth": len(_bronny_cmd_queue)}


@app.get("/youtube/search")
async def youtube_search(q: str, limit: int = 12):
    if not shutil.which("yt-dlp"):
        raise HTTPException(status_code=501, detail="yt-dlp not installed on server")
    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", f"ytsearch{limit}:{q}", "--flat-playlist",
            "--print",
            '{"videoId":"%(id)s","title":"%(title)s","channel":"%(uploader)s",'
            '"duration":%(duration)s,"thumb":"https://img.youtube.com/vi/%(id)s/mqdefault.jpg"}',
            "--no-warnings", "--quiet",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
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
    if not shutil.which("yt-dlp"):
        raise HTTPException(status_code=501, detail="yt-dlp not installed on server")
    if mode == "audio":
        args = ["yt-dlp", url, "-f", "bestaudio[ext=mp3]/bestaudio/best",
                "-x", "--audio-format", "mp3", "--audio-quality", "5",
                "-o", "-", "--no-playlist", "--quiet", "--no-warnings"]
        media_type = "audio/mpeg"
    else:
        args = ["yt-dlp", url, "-f", "best[height<=480]/best",
                "-o", "-", "--no-playlist", "--quiet", "--no-warnings"]
        media_type = "video/mp4"

    async def stream_media():
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
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

    return StreamingResponse(stream_media(), media_type=media_type,
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

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
            files.append({"name": f.name,
                           "size_kb": round(f.stat().st_size / 1024, 1),
                           "created": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                           "url": f"/files/download/{f.name}"})
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
    output_dir.mkdir(exist_ok=True)
    count = 0
    for f in output_dir.iterdir():
        if f.is_file():
            f.unlink()
            count += 1
    return {"deleted_count": count}

# ── Preferences endpoints ─────────────────────────────────────────────────────

@app.get("/preferences")
async def list_preferences():
    try:
        from agents.memory_agent import MemoryAgent, _INDEX_KEY
        index = memory.get_preference(_INDEX_KEY, default=[])
        prefs = []
        if isinstance(index, list):
            for key in index:
                val = memory.get_preference(key)
                if val is not None:
                    prefs.append({"key": key, "value": val})
        return {"preferences": prefs}
    except Exception as e:
        return {"preferences": [], "error": str(e)}

@app.post("/preferences")
async def save_preference(req: PrefRequest):
    from agents.memory_agent import _INDEX_KEY
    key = req.label.lower().replace(" ", "_")
    memory.set_preference(key, {"label": req.label, "value": req.value})
    index = memory.get_preference(_INDEX_KEY, default=[])
    if not isinstance(index, list):
        index = []
    if key not in index:
        index.append(key)
        memory.set_preference(_INDEX_KEY, index)
    return {"key": key, "label": req.label, "value": req.value, "saved": True}

@app.delete("/preferences/all")
async def clear_all_preferences():
    from agents.memory_agent import _INDEX_KEY
    index = memory.get_preference(_INDEX_KEY, default=[])
    if isinstance(index, list):
        for key in index:
            memory.delete_preference(key)
    memory.set_preference(_INDEX_KEY, [])
    return {"cleared": True}

@app.delete("/preferences/{key:path}")
async def delete_preference(key: str):
    from agents.memory_agent import _INDEX_KEY
    memory.delete_preference(key)
    index = memory.get_preference(_INDEX_KEY, default=[])
    if isinstance(index, list) and key in index:
        index.remove(key)
        memory.set_preference(_INDEX_KEY, index)
    return {"key": key, "deleted": True}

# ── Devices endpoint ──────────────────────────────────────────────────────────

@app.get("/devices")
async def list_devices():
    return {"devices": ws_manager.list_devices()}

# ── WebSocket — Device Agents ─────────────────────────────────────────────────

@app.websocket("/ws/device/{device_id}")
async def device_websocket(websocket: WebSocket, device_id: str):
    if settings.API_KEY:
        if websocket.query_params.get("api_key", "") != settings.API_KEY:
            await websocket.close(code=4401, reason="Invalid API key")
            return
    await ws_manager.connect_device(device_id, websocket)
    try:
        while True:
            data = json.loads(await websocket.receive_text())
            await ws_manager.handle_device_message(device_id, data)
    except WebSocketDisconnect:
        ws_manager.disconnect_device(device_id)

# ── WebSocket — Web UI ────────────────────────────────────────────────────────

@app.websocket("/ws/ui/{session_id}")
async def ui_websocket(websocket: WebSocket, session_id: str):
    if websocket.query_params.get("token", "") != _UI_SESSION_TOKEN:
        await websocket.close(code=4401, reason="Invalid session token")
        return
    await ws_manager.connect_ui(session_id, websocket)
    try:
        while True:
            await asyncio.sleep(30)
            await websocket.send_text(json.dumps({"type": "ping"}))
    except (WebSocketDisconnect, RuntimeError):
        ws_manager.disconnect_ui(session_id)

# ── Static files ──────────────────────────────────────────────────────────────

try:
    app.mount("/ui", StaticFiles(directory="../web_ui", html=True), name="ui")
except Exception:
    pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
