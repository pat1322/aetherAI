"""
AetherAI — WebSocket Manager  (Stage 6 — Layer 1)

Stage 6 addition
────────────────
STREAM 1  stream_chunk_to_ui() — lightweight helper that broadcasts a
          single streaming token to all connected UI sessions without
          going through the broadcast_task_update() deduplication cache.
          The dedup cache is designed for full status updates (same status
          message repeated twice); it must not suppress stream chunks
          which are all unique but often very short (1-3 chars).

All Stage 5 fixes retained:
  FIX A  asyncio.get_running_loop() throughout (no get_event_loop())
  FIX B  start() called from FastAPI lifespan, not __init__
  FIX C  disconnect_device() schedules UI broadcast via call_soon_threadsafe
"""

import asyncio
import enum
import json
import logging
import time
from typing import Callable

from fastapi import WebSocket

logger = logging.getLogger(__name__)

UI_QUEUE_MAX   = 64
PENDING_TTL    = 120.0
PRUNE_INTERVAL = 60.0


class SendResult(enum.Enum):
    OK         = "ok"
    NO_DEVICE  = "no_device"
    SEND_ERROR = "send_error"


class WebSocketManager:

    def __init__(self):
        self._devices:         dict[str, WebSocket]      = {}
        self._ui_sessions:     dict[str, WebSocket]      = {}
        self._ui_queues:       dict[str, asyncio.Queue]  = {}
        self._ui_writers:      dict[str, asyncio.Task]   = {}
        self._pending:         dict[str, asyncio.Future] = {}
        self._pending_ts:      dict[str, float]          = {}
        self._vision_handlers: dict[str, Callable]       = {}
        self._last_broadcast:  dict[str, str]            = {}
        self._prune_task:      asyncio.Task | None       = None

    async def start(self):
        """FIX B: call from FastAPI lifespan after the event loop is running."""
        loop = asyncio.get_running_loop()
        self._prune_task = loop.create_task(self._background_prune())
        logger.info("[WSManager] Background prune task started")

    # ── Devices ────────────────────────────────────────────────────────────────

    async def connect_device(self, device_id: str, ws: WebSocket):
        await ws.accept()
        self._devices[device_id] = ws
        logger.info(f"[WSManager] Device connected: {device_id}")
        await self.broadcast_ui_event({"type": "device_connected", "device_id": device_id})

    def disconnect_device(self, device_id: str):
        self._devices.pop(device_id, None)
        logger.info(f"[WSManager] Device disconnected: {device_id}")
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                self.broadcast_ui_event({"type": "device_disconnected", "device_id": device_id})
            )
        except RuntimeError:
            pass

    def list_devices(self) -> list[str]:
        return list(self._devices.keys())

    def device_count(self) -> int:
        return len(self._devices)

    async def send_to_device(self, device_id: str, data: dict) -> SendResult:
        ws = self._devices.get(device_id)
        if not ws:
            logger.warning(f"[WSManager] Device '{device_id}' not connected")
            return SendResult.NO_DEVICE
        try:
            await ws.send_text(json.dumps(data))
            return SendResult.OK
        except Exception as e:
            logger.error(f"[WSManager] Send to '{device_id}' failed: {e}")
            self.disconnect_device(device_id)
            return SendResult.SEND_ERROR

    # ── UI sessions ────────────────────────────────────────────────────────────

    async def connect_ui(self, session_id: str, ws: WebSocket):
        await ws.accept()
        self._ui_sessions[session_id] = ws
        q = asyncio.Queue(maxsize=UI_QUEUE_MAX)
        self._ui_queues[session_id]  = q
        self._ui_writers[session_id] = asyncio.get_running_loop().create_task(
            self._session_writer(session_id, ws, q)
        )
        logger.info(f"[WSManager] UI session connected: {session_id}")

    def disconnect_ui(self, session_id: str):
        self._ui_sessions.pop(session_id, None)
        self._ui_queues.pop(session_id, None)
        writer = self._ui_writers.pop(session_id, None)
        if writer and not writer.done():
            writer.cancel()

    async def _session_writer(self, session_id: str, ws: WebSocket, q: asyncio.Queue):
        try:
            while True:
                payload = await q.get()
                try:
                    await ws.send_text(payload)
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    async def broadcast_ui_event(self, data: dict):
        payload = json.dumps(data)
        dead    = []
        for sid, q in list(self._ui_queues.items()):
            if self._ui_sessions.get(sid) is None:
                dead.append(sid)
                continue
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass
        for sid in dead:
            self.disconnect_ui(sid)

    async def broadcast_task_update(self, task_id: str, data: dict):
        payload = {"type": "task_update", "task_id": task_id, **data}
        key     = json.dumps(payload, sort_keys=True)
        if self._last_broadcast.get(task_id) == key:
            return
        self._last_broadcast[task_id] = key
        await self.broadcast_ui_event(payload)

    def clear_broadcast_cache(self, task_id: str):
        self._last_broadcast.pop(task_id, None)

    # ── Streaming helper (Stage 6) ─────────────────────────────────────────────

    async def stream_chunk_to_ui(self, task_id: str, chunk: str):
        """
        STREAM 1 — Broadcast a single streaming token to all UI sessions.

        Bypasses the broadcast_task_update() deduplication cache intentionally:
        the dedup cache is keyed on the full payload JSON and is designed to
        suppress repeated identical status messages, not individual token chunks.
        Calling broadcast_task_update() for every token would (a) pollute the
        dedup cache with thousands of entries, and (b) incorrectly suppress any
        two consecutive identical tokens (e.g. "  " whitespace pairs).

        This method calls broadcast_ui_event() directly — same delivery path,
        no dedup, no cache mutation.
        """
        await self.broadcast_ui_event({
            "type":    "stream_chunk",
            "task_id": task_id,
            "chunk":   chunk,
        })

    # ── Pending futures ────────────────────────────────────────────────────────

    def register_pending(self, request_id: str, future: asyncio.Future):
        self._pending[request_id]    = future
        self._pending_ts[request_id] = time.monotonic()

    def unregister_pending(self, request_id: str):
        self._pending.pop(request_id, None)
        self._pending_ts.pop(request_id, None)
        self._vision_handlers.pop(request_id, None)

    def register_vision_task(self, request_id: str, future: asyncio.Future,
                              handler: Callable):
        self.register_pending(request_id, future)
        self._vision_handlers[request_id] = handler

    # ── Device message routing ─────────────────────────────────────────────────

    async def handle_device_message(self, device_id: str, data: dict):
        msg_type   = data.get("type")
        request_id = data.get("request_id", "")

        def _resolve(result):
            f = self._pending.get(request_id)
            if f and not f.done():
                f.set_result(result)

        if msg_type in ("action_result", "screenshot_result", "screen_info"):
            if request_id in self._pending:
                _resolve(data)

        elif msg_type == "vision_step" and request_id in self._vision_handlers:
            handler = self._vision_handlers[request_id]
            try:
                action = await handler(device_id, request_id, data)
                await self.send_to_device(device_id, {
                    "type": "vision_action", "request_id": request_id, **action,
                })
            except Exception as e:
                logger.error(f"[WSManager] Vision handler error: {e}")
                await self.send_to_device(device_id, {
                    "type": "vision_action", "request_id": request_id,
                    "action": "done", "message": f"Handler error: {e}",
                })

        elif msg_type == "vision_complete":
            f = self._pending.get(request_id)
            if f and not f.done():
                f.set_result(data.get("message", "Done"))
            self._vision_handlers.pop(request_id, None)

        elif msg_type in ("status", "log", "error"):
            await self.broadcast_ui_event({
                "type": "device_log", "device_id": device_id,
                "message": data.get("message", ""), "level": data.get("level", "info"),
            })

        elif msg_type == "ping":
            await self.send_to_device(device_id, {"type": "pong"})

    # ── Background maintenance ─────────────────────────────────────────────────

    async def _background_prune(self):
        while True:
            await asyncio.sleep(PRUNE_INTERVAL)
            try:
                self._prune_dead_ui_sessions()
                self._purge_stale_pending()
            except Exception as e:
                logger.warning(f"[WSManager] Prune error: {e}")

    def _prune_dead_ui_sessions(self):
        dead = [
            sid for sid, ws in list(self._ui_sessions.items())
            if getattr(ws, "client_state", None) is not None
            and ws.client_state.value >= 3
        ]
        for sid in dead:
            self.disconnect_ui(sid)
        if dead:
            logger.debug(f"[WSManager] Pruned {len(dead)} dead UI sessions")

    def _purge_stale_pending(self):
        now   = time.monotonic()
        stale = [
            rid for rid, ts in list(self._pending_ts.items())
            if now - ts > PENDING_TTL
        ]
        for rid in stale:
            f = self._pending.get(rid)
            if f and not f.done():
                f.set_result({"error": "timeout", "request_id": rid})
                logger.warning(f"[WSManager] Expired stale pending: {rid}")
            self.unregister_pending(rid)
        if stale:
            logger.info(f"[WSManager] Purged {len(stale)} stale pending futures")
