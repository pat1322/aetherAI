"""
AetherAI Cloud Brain — Stage 5
FastAPI entry point: API Gateway + WebSocket manager + Preference endpoints
"""

from dotenv import load_dotenv
from pathlib import Path
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

import asyncio
import json
import uuid
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from orchestrator import Orchestrator
from memory import MemoryManager
from utils.websocket_manager import WebSocketManager
from config import settings

# ── App setup ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="AetherAI Cloud Brain",
    description="Personal AI Agent System — Stage 5",
    version="5.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ── Global instances ──────────────────────────────────────────────────────────

ws_manager   = WebSocketManager()
memory       = MemoryManager()
orchestrator = Orchestrator(memory=memory, ws_manager=ws_manager)

# ── Request / Response models ─────────────────────────────────────────────────

class CommandRequest(BaseModel):
    command: str
    source: str = "web"
    session_id: Optional[str] = None

class CommandResponse(BaseModel):
    task_id: str
    status: str
    message: str
    plan: Optional[list] = None

class StatusResponse(BaseModel):
    task_id: str
    status: str
    steps: list
    result: Optional[str] = None
    created_at: str
    updated_at: str

class PrefRequest(BaseModel):
    label: str
    value: str

# ── Core REST endpoints ───────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"system": "AetherAI Cloud Brain", "status": "online", "version": "5.0.0"}

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "devices_connected": ws_manager.device_count(),
        "task_stats": memory.get_task_stats(),
    }

@app.post("/command", response_model=CommandResponse)
async def receive_command(req: CommandRequest):
    task_id = str(uuid.uuid4())
    memory.create_task(task_id, req.command, req.source)
    asyncio.create_task(orchestrator.run_task(task_id, req.command))
    return CommandResponse(
        task_id=task_id,
        status="started",
        message=f"Task received. AetherAI is working on: '{req.command}'",
    )

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
    success = orchestrator.cancel_task(task_id)
    if not success:
        raise HTTPException(status_code=404, detail="Task not found or already finished")
    memory.update_task_status(task_id, "cancelled")
    return {"task_id": task_id, "status": "cancelled"}

@app.delete("/task/{task_id}")
async def delete_task(task_id: str):
    success = memory.delete_task(task_id)
    if not success:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task_id": task_id, "deleted": True}

@app.delete("/tasks/all")
async def delete_all_tasks():
    count = memory.delete_all_tasks()
    return {"deleted_count": count}

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

@app.delete("/files/all/clear")
async def delete_all_files():
    output_dir = Path(__file__).parent.parent / "output"
    count = sum(1 for f in output_dir.iterdir() if f.is_file() and not f.unlink())
    return {"deleted_count": count}

# ── Preference endpoints (Stage 5) ───────────────────────────────────────────

@app.get("/preferences")
async def list_preferences():
    """Return all stored user preferences."""
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
    """Save a preference directly (bypasses the memory agent NLP)."""
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
    """Wipe all stored preferences."""
    from agents.memory_agent import _INDEX_KEY
    index = memory.get_preference(_INDEX_KEY, default=[])
    if isinstance(index, list):
        for key in index:
            memory.set_preference(key, None)
    memory.set_preference(_INDEX_KEY, [])
    return {"cleared": True}

@app.delete("/preferences/{key:path}")
async def delete_preference(key: str):
    """Delete a preference by its key slug."""
    from agents.memory_agent import _INDEX_KEY
    memory.set_preference(key, None)
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
    await ws_manager.connect_device(device_id, websocket)
    try:
        while True:
            raw  = await websocket.receive_text()
            data = json.loads(raw)
            await ws_manager.handle_device_message(device_id, data)
    except WebSocketDisconnect:
        ws_manager.disconnect_device(device_id)

# ── WebSocket — Web UI ────────────────────────────────────────────────────────

@app.websocket("/ws/ui/{session_id}")
async def ui_websocket(websocket: WebSocket, session_id: str):
    await ws_manager.connect_ui(session_id, websocket)
    try:
        while True:
            await asyncio.sleep(30)
            await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        ws_manager.disconnect_ui(session_id)

# ── Static files (Web UI) ─────────────────────────────────────────────────────

try:
    app.mount("/ui", StaticFiles(directory="../web_ui", html=True), name="ui")
except Exception:
    pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
