# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

AetherAI is a cloud-hosted AI agent platform consisting of four components:
- **`cloud_brain/`** — FastAPI backend (Python), deployed to Railway
- **`device_agent/`** — Windows PC control client (Python, WebSocket)
- **`esp32_voice_agent/`** — ESP32-S3 voice device firmware (Arduino/C++)
- **`web_ui/`** — Single-file SPA (`index.html`, no build step)

Current version: **Stage 7** (Bronny voice identity, v7.0)

## Commands

### Cloud Brain (primary server)
```bash
pip install -r requirements.txt
playwright install chromium
cd cloud_brain
uvicorn main:app --reload --port 8000
# UI: http://localhost:8000/ui/index.html
```

### Device Agent (Windows PC control)
```bash
cd device_agent
python agent.py
```

### ESP32 Firmware
1. Copy `esp32_voice_agent/voice_config.h.example` → `voice_config.h`, fill in credentials
2. Open `bronny_ai.ino` in Arduino IDE, select ESP32S3 Dev Module, upload

### Railway Deployment
```bash
git push origin main
# Railway auto-builds via nixpacks.toml
# Procfile starts: cd cloud_brain && uvicorn main:app --host 0.0.0.0 --port $PORT
```

## Architecture

### Request Flow

```
User input (web UI or voice)
    ↓
POST /command or /stream
    ↓
classify_command()  →  "chat": stream_answer() → tokens to UI
                    →  "task": orchestrator.run_task()
                                    ↓
                              plan multi-step task
                                    ↓
                              agent_router.execute_step()
                              (one of 11 specialized agents)
                                    ↓
                              stream chunks via WebSocket to UI
```

### Voice Pipeline (ESP32 → Cloud)
```
INMP441 mic → Deepgram Nova-2 WebSocket (on-device ASR) → POST /voice/text
→ Qwen LLM → _voice_summarize() (≤600 chars) → edge-tts → MP3 → ESP32 speaker
```

### Key Files

| File | Purpose |
|------|---------|
| `cloud_brain/main.py` | FastAPI app — all endpoints, voice pipeline |
| `cloud_brain/orchestrator.py` | Multi-step task planner and streaming orchestration |
| `cloud_brain/agent_router.py` | Instantiates and dispatches to the right agent |
| `cloud_brain/agents/__init__.py` | `BaseAgent` class with `stream_llm()` / `stream_context()` |
| `cloud_brain/utils/qwen_client.py` | LLM wrapper (chat, vision, streaming) |
| `cloud_brain/utils/tts_client.py` | edge-tts synthesis (no API key needed) |
| `cloud_brain/memory.py` | SQLite persistence (tasks + user preferences) |
| `device_agent/agent.py` | PC control WebSocket client (pyautogui, pywinauto) |

### Agents (11 total, all in `cloud_brain/agents/`)
`research`, `browser`, `document`, `coding`, `automation`, `memory`, `weather`, `crypto`, `news`, `finance`, `voice`

Each extends `BaseAgent` and uses `stream_llm()` to push tokens directly to the WebSocket.

### Streaming
- **WebSocket** `/ws/ui/{id}` — live token streaming to browser
- **WebSocket** `/ws/device/{id}` — PC control commands
- **SSE** `/stream` — fallback for environments without WebSocket
- `WebSocketManager` in `utils/websocket_manager.py` maintains connection pool

### Database
SQLite with WAL mode at `DB_PATH` (default: `../database/aether.db`, Railway: `/data/aether.db`). Uses synchronous `sqlite3` (not async). Two tables: `tasks` and `preferences` (key-value, auto-injected into LLM prompts).

## Environment Variables

| Variable | Notes |
|----------|-------|
| `QWEN_API_KEY` | Required — DashScope key (used for both LLM and Paraformer ASR) |
| `AETHER_API_KEY` | Recommended in prod — checked via `X-Api-Key` header |
| `DB_PATH` | Set to `/data/aether.db` on Railway (persistent volume) |
| `QWEN_MODEL` | Default: `qwen-turbo-latest` |
| `TTS_VOICE` | Default: `en-US-AriaNeural` |
| `GNEWS_API_KEY`, `ALPHAVANTAGE_API_KEY`, `SERPER_API_KEY` | Optional agent keys |
| `TASK_RETENTION_DAYS` | Default: `30` |
| `MAX_CONCURRENT_TASKS` | Default: `5` |

Copy `.env.example` → `.env` for local dev. Never commit `.env`, `voice_config.h`, or `aether_config.ini`.

## Design Decisions

- **Qwen via DashScope** (not OpenAI) — lower cost, same OpenAI-compatible API
- **edge-tts** for TTS — free Microsoft neural voices, no key, direct MP3 output
- **Single `index.html`** for web UI — no build pipeline, inline CSS/JS
- **Voice responses capped at ~600 chars** — keeps audio playback under ~10 seconds on ESP32
- **pyautogui `FAILSAFE=True`** on device agent — move mouse to top-left corner to abort automation
