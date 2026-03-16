"""
AetherAI Cloud Brain — Stage 6 (Layer 1 + Layer 2)

Stage 6 Layer 2 additions
──────────────────────────
VOICE 1  POST /voice/chat
         Accepts raw WAV audio bytes (Content-Type: audio/wav).
         Pipeline: Paraformer STT → Qwen LLM → edge-tts → MP3 bytes.
         Response headers carry X-Transcript and X-Response-Text so the
         ESP32 can display what was heard / said on the TFT screen.

VOICE 2  GET /tts/voices
         Returns the list of available edge-tts voices — useful for a
         future settings UI or to verify the voice name is valid.

Stage 6 Layer 1 retained:
  SSE 1   POST /stream  — Server-Sent Events streaming for chat commands

All Stage 5 fixes retained:
  FIX 12  DELETE /files/all/clear before DELETE /files/{filename}
  FIX 15  MAX_CONCURRENT_TASKS cap
"""

from contextlib import asynccontextmanager
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

import asyncio
import json
import os
import secrets
import uuid
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

_UI_SESSION_TOKEN = secrets.token_urlsafe(24)
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "5"))

# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await ws_manager.start()
    yield

# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(
    title="AetherAI Cloud Brain",
    description="Personal AI Agent System — Stage 6",
    version="6.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ── API key middleware ────────────────────────────────────────────────────────

_PUBLIC_PREFIXES = ("/ui", "/health", "/docs", "/openapi", "/redoc", "/files/download")
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
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing API key. Set X-Api-Key header."},
            )
        return await call_next(request)

app.add_middleware(ApiKeyMiddleware)

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

# ── Core endpoints ────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"system": "AetherAI Cloud Brain", "status": "online", "version": "6.0.0"}

@app.get("/health")
async def health():
    return {
        "status":           "healthy",
        "timestamp":        datetime.utcnow().isoformat(),
        "devices_connected": ws_manager.device_count(),
        "task_stats":       memory.get_task_stats(),
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
            detail=f"Too many tasks in progress. Wait for one to finish.",
        )
    task_id = str(uuid.uuid4())
    memory.create_task(task_id, req.command, req.source)
    asyncio.create_task(orchestrator.run_task(task_id, req.command))
    return CommandResponse(
        task_id=task_id, status="started",
        message=f"Task received: '{req.command}'",
    )

# ── SSE streaming endpoint (Layer 1) ─────────────────────────────────────────

def _load_user_ctx() -> str:
    try:
        from agents.memory_agent import MemoryAgent
        return MemoryAgent.load_context(memory)
    except Exception:
        return ""

@app.post("/stream")
async def stream_command(req: CommandRequest):
    """
    SSE endpoint — streams chat tokens directly, or returns task_id for
    task-classified commands.
    """
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
            task_id = str(uuid.uuid4())
            memory.create_task(task_id, req.command, req.source)
            asyncio.create_task(orchestrator.run_task(task_id, req.command))
            yield f"data: {json.dumps({'type': 'task_created', 'task_id': task_id})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ── Voice endpoints (Layer 2) ─────────────────────────────────────────────────

@app.post("/voice/chat")
async def voice_chat(request: Request):
    """
    VOICE 1 — ESP32 voice pipeline endpoint.

    Request:
        Content-Type: audio/wav
        Body:         Raw WAV audio bytes (16kHz mono 16-bit from INMP441)
        X-Api-Key:    AETHER_API_KEY

    Response:
        Content-Type:    audio/mpeg
        X-Transcript:    What the user said (UTF-8, URL-encoded)
        X-Response-Text: Short spoken response text (UTF-8, URL-encoded)
        Body:            MP3 audio bytes ready to feed to the ES8311

    On error: returns JSON {"detail": "..."} with appropriate HTTP status.
    """
    from urllib.parse import quote as urlquote

    audio_bytes = await request.body()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio body")

    if len(audio_bytes) < 100:
        raise HTTPException(status_code=400, detail="Audio too short")

    try:
        from agents.voice_agent import process_voice
        result = await process_voice(
            audio_bytes=audio_bytes,
            qwen=orchestrator.qwen,
            memory=memory,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Voice pipeline error: {e}")

    if not result.mp3_bytes:
        raise HTTPException(
            status_code=502,
            detail="TTS produced no audio. Check TTS_VOICE env var and edge-tts installation.",
        )

    # URL-encode headers so non-ASCII (Filipino, etc.) survives HTTP transport
    return Response(
        content=result.mp3_bytes,
        media_type="audio/mpeg",
        headers={
            "X-Transcript":     urlquote(result.transcript[:300]),
            "X-Response-Text":  urlquote(result.spoken_text[:300]),
            "Cache-Control":    "no-cache",
        },
    )


@app.get("/tts/voices")
async def tts_voices():
    """
    VOICE 2 — List all available edge-tts voices.
    Useful for choosing a voice in settings or verifying TTS_VOICE is valid.
    """
    from utils.tts_client import list_voices
    voices = await list_voices()
    return {
        "current_voice": settings.TTS_VOICE,
        "voices":        voices,
    }

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
    fpath = Path(__file__).parent.parent / "output" / filename
    if not fpath.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path=str(fpath), filename=filename)

@app.delete("/files/all/clear")
async def delete_all_files():
    output_dir = Path(__file__).parent.parent / "output"
    count = sum(1 for f in output_dir.iterdir() if f.is_file() and not f.unlink())
    return {"deleted_count": count}

@app.delete("/files/{filename}")
async def delete_file(filename: str):
    import re as _re
    if not _re.match(r'^[\w\-. ]+$', filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    fpath = Path(__file__).parent.parent / "output" / filename
    if not fpath.exists():
        raise HTTPException(status_code=404, detail="File not found")
    fpath.unlink()
    return {"filename": filename, "deleted": True}

# ── Preference endpoints ──────────────────────────────────────────────────────

@app.get("/preferences")
async def list_preferences():
    from agents.memory_agent import _INDEX_KEY
    index = memory.get_preference(_INDEX_KEY, default=[])
    if not isinstance(index, list):
        return {"preferences": []}
    prefs = []
    for key in index:
        entry = memory.get_preference(key)
        if entry and isinstance(entry, dict) and entry.get("label"):
            prefs.append({"key": key, "label": entry["label"], "value": entry["value"]})
    return {"preferences": prefs}

@app.post("/preferences")
async def set_preference_api(req: PrefRequest):
    from agents.memory_agent import _INDEX_KEY, _slug, _PREF_PREFIX
    key   = _PREF_PREFIX + _slug(req.label)
    entry = {"label": req.label, "value": req.value, "raw": f"{req.label}: {req.value}"}
    memory.set_preference(key, entry)
    index = memory.get_preference(_INDEX_KEY, default=[])
    if not isinstance(index, list): index = []
    if key not in index:
        index.append(key)
        memory.set_preference(_INDEX_KEY, index)
    return {"key": key, "label": req.label, "value": req.value, "saved": True}

@app.delete("/preferences/all")
async def clear_preferences():
    from agents.memory_agent import _INDEX_KEY
    index = memory.get_preference(_INDEX_KEY, default=[])
    if isinstance(index, list):
        for key in index: memory.delete_preference(key)
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

# ── Device list ───────────────────────────────────────────────────────────────

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
