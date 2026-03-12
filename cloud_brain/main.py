"""
AetherAI Cloud Brain — Stage 1
FastAPI entry point: API Gateway + WebSocket manager
"""

# Load .env file FIRST before anything else imports config
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

import asyncio
import json
import uuid
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
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
    description="Personal AI Agent System",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Global instances ──────────────────────────────────────────────────────────

ws_manager = WebSocketManager()
memory = MemoryManager()
orchestrator = Orchestrator(memory=memory, ws_manager=ws_manager)

# ── Request / Response models ─────────────────────────────────────────────────

class CommandRequest(BaseModel):
    command: str
    source: str = "web"          # web | voice | device
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

# ── Auth dependency (simple API key) ─────────────────────────────────────────

async def verify_api_key(x_api_key: Optional[str] = None):
    if settings.API_KEY and x_api_key != settings.API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return True

# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"system": "AetherAI Cloud Brain", "status": "online", "version": "1.0.0"}

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "task_stats": memory.get_task_stats(),
        "timestamp": datetime.utcnow().isoformat(),
        "devices_connected": ws_manager.device_count(),
    }

@app.post("/command", response_model=CommandResponse)
async def receive_command(req: CommandRequest):
    """
    Main endpoint. Receives a natural language command,
    creates a task, and starts orchestration.
    """
    task_id = str(uuid.uuid4())

    # Store initial task record
    memory.create_task(task_id, req.command, req.source)

    # Kick off orchestration (non-blocking)
    orchestrator.start_task(task_id, req.command)

    return CommandResponse(
        task_id=task_id,
        status="started",
        message=f"Task received. AetherAI is working on: '{req.command}'",
    )

@app.get("/task/{task_id}", response_model=StatusResponse)
async def get_task_status(task_id: str):
    """Returns full status + step log for a task."""
    task = memory.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task

@app.get("/tasks")
async def list_tasks(limit: int = 20):
    """Returns recent tasks."""
    return memory.list_tasks(limit=limit)

@app.post("/task/{task_id}/cancel")
async def cancel_task(task_id: str):
    """Cancel a running task."""
    success = orchestrator.cancel_task(task_id)
    if not success:
        raise HTTPException(status_code=404, detail="Task not found or already finished")
    memory.update_task_status(task_id, "cancelled")
    return {"task_id": task_id, "status": "cancelled"}


@app.delete("/task/{task_id}")
async def delete_task(task_id: str):
    """Delete a task and its steps from the database."""
    success = memory.delete_task(task_id)
    if not success:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task_id": task_id, "deleted": True}

@app.delete("/tasks/all")
async def delete_all_tasks():
    """Delete all tasks from the database."""
    count = memory.delete_all_tasks()
    return {"deleted_count": count}

@app.delete("/files/{filename}")
async def delete_file(filename: str):
    """Delete a generated file."""
    import re
    from pathlib import Path as FilePath
    if not re.match(r'^[\w\-. ]+$', filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    fpath = FilePath(__file__).parent.parent / "output" / filename
    if not fpath.exists():
        raise HTTPException(status_code=404, detail="File not found")
    fpath.unlink()
    return {"filename": filename, "deleted": True}

@app.delete("/files/all/clear")
async def delete_all_files():
    """Delete all generated files."""
    from pathlib import Path as FilePath
    output_dir = FilePath(__file__).parent.parent / "output"
    count = 0
    for f in output_dir.iterdir():
        if f.is_file():
            f.unlink()
            count += 1
    return {"deleted_count": count}

@app.get("/files")
async def list_files():
    """List all generated output files."""
    from pathlib import Path as FilePath
    output_dir = FilePath(__file__).parent.parent / "output"
    output_dir.mkdir(exist_ok=True)
    files = []
    for f in sorted(output_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file():
            files.append({
                "name": f.name,
                "size_kb": round(f.stat().st_size / 1024, 1),
                "created": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "url": f"/files/download/{f.name}",
            })
    return {"files": files}

@app.get("/files/download/{filename}")
async def download_file(filename: str):
    """Download a generated file."""
    import re
    from pathlib import Path as FilePath
    from fastapi.responses import FileResponse as FileResp
    if not re.match(r'^[\w\-. ]+$', filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    fpath = FilePath(__file__).parent.parent / "output" / filename
    if not fpath.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResp(path=str(fpath), filename=filename)

@app.get("/devices")
async def list_devices():
    """Returns connected device agents."""
    return {"devices": ws_manager.list_devices()}

# ── WebSocket endpoint for Device Agents ─────────────────────────────────────

@app.websocket("/ws/device/{device_id}")
async def device_websocket(websocket: WebSocket, device_id: str):
    """
    Persistent WebSocket connection for desktop/laptop Device Agents.
    Device Agent connects here and waits for action commands.
    """
    await ws_manager.connect_device(device_id, websocket)
    try:
        while True:
            # Listen for messages from the device (e.g. screenshot results)
            raw = await websocket.receive_text()
            data = json.loads(raw)
            await ws_manager.handle_device_message(device_id, data)
    except WebSocketDisconnect:
        ws_manager.disconnect_device(device_id)

# ── WebSocket endpoint for Web UI live updates ────────────────────────────────

@app.websocket("/ws/ui/{session_id}")
async def ui_websocket(websocket: WebSocket, session_id: str):
    """
    Web UI connects here to receive live task progress updates.
    """
    await ws_manager.connect_ui(session_id, websocket)
    try:
        while True:
            await asyncio.sleep(30)  # keep-alive ping
            await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        ws_manager.disconnect_ui(session_id)

# ── Static files (Web UI) ─────────────────────────────────────────────────────

try:
    app.mount("/ui", StaticFiles(directory="../web_ui", html=True), name="ui")
except Exception:
    pass  # Web UI folder may not exist in dev

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
