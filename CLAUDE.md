# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## What This Project Is

AetherAI is a cloud-hosted AI agent platform with four components:

- **`cloud_brain/`** — FastAPI backend (Python), deployed to Railway
- **`device_agent/`** — Windows PC control client (Python, WebSocket)
- **`esp32_voice_agent/`** — ESP32-S3 voice device firmware (Arduino/C++)
- **`web_ui/`** — Single-file SPA (`index.html`, no build step)

Current version: **Stage 7 — Cloud Brain v7.0 · Bronny AI v1.1**

---

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
3. Board settings: OPI PSRAM (8MB), USB CDC on Boot: Enabled

### Railway Deployment
```bash
git push origin main
# Railway auto-builds via nixpacks.toml
# Procfile starts: cd cloud_brain && uvicorn main:app --host 0.0.0.0 --port $PORT
```

---

## Architecture

### Request Flow

```
User input (web UI or voice)
    ↓
POST /command  or  POST /stream
    ↓
classify_command()  →  "chat": stream_answer() → tokens to UI (SSE/WS)
                    →  "task": orchestrator.run_task()
                                    ↓
                              plan_task() → list of steps
                                    ↓
                              agent_router.execute_step()
                              (one of 11 specialised agents)
                                    ↓
                              stream chunks via WebSocket to UI
```

### Voice Pipeline (ESP32 → Cloud)

```
INMP441 mic (16kHz mono, 32-bit I2S, channels=1, MIC_GAIN_SHIFT=12)
    ↓
Deepgram Nova-3 WebSocket (persistent WSS to api.deepgram.com)
    interim_results=true, endpointing=350ms VAD
    speech_final event → transcript ready
    ↓
POST /voice/text  (pre-transcribed text, no WAV upload)
    ↓
classify_command() → agent or bronny_answer() (Qwen LLM with Bronny persona)
    ↓
_voice_summarize() (≤600 chars, no markdown)
    ↓
edge-tts → MP3 bytes streamed back (STREAM_DATA_GAP_MS=500ms timeout)
    ↓
MP3DecoderHelix → ES8311 codec → speaker
    ↓
Deepgram streaming resumes
```

### Streaming Architecture (Stage 6)

Two kinds of streaming are interleaved in the WebSocket:

- **Chat streaming** (`stream_event: stream_start / chunk / stream_end`) — the entire task is one stream; `unlockInput()` is called on end.
- **Agent streaming** (`stream_event: agent_stream_start / chunk / agent_stream_end`) — one step inside a multi-step task; `unlockInput()` is NOT called because more steps may follow.

`stream_chunk` messages bypass the broadcast dedup cache (which is designed for full status messages) to prevent chunk suppression.

Each stream is keyed by `task_id + ':' + step_num` so concurrent steps never share buffers.

### Key Files

| File | Purpose |
|------|---------|
| `cloud_brain/main.py` | FastAPI app — all endpoints, Bronny heartbeat, voice pipeline |
| `cloud_brain/orchestrator.py` | Streaming chat + task planning, agent stream open/close events |
| `cloud_brain/agent_router.py` | Instantiates and dispatches to the right agent |
| `cloud_brain/agents/__init__.py` | `BaseAgent` — `stream_llm()` / `stream_summarize()` / `set_stream_context()` |
| `cloud_brain/utils/qwen_client.py` | LLM wrapper — chat, streaming, `bronny_answer()`, `stream_answer()` |
| `cloud_brain/utils/tts_client.py` | edge-tts synthesis — `_clean_for_speech()`, `synthesize()`, `list_voices()` |
| `cloud_brain/memory.py` | SQLite — WAL, FK constraints, indexes, `delete_preference()` real DELETE |
| `device_agent/agent.py` | PC control — pyautogui, pywinauto, vision loop, Office COM automation |
| `esp32_voice_agent/bronny_ai.ino` | Bronny AI v1.1 firmware — all face states, Deepgram WS, TTS playback |

### Agents (11 total, all in `cloud_brain/agents/`)

`research`, `browser`, `document`, `coding`, `automation`, `memory`, `weather`, `crypto`, `news`, `finance`, `voice`

Each extends `BaseAgent`. Streaming agents (`research_agent`, `browser_agent`) use `stream_llm()` / `stream_summarize()` for their final write step. `coding_agent` and `news_agent` are excluded from streaming to avoid double-render artefacts.

### STREAMING_AGENTS (orchestrator.py)

```python
STREAMING_AGENTS = frozenset({
    "research_agent",
    "browser_agent",
})
```

Do not add `coding_agent` (returns `[CODE_BLOCK:...]` tag — streaming causes double-render) or `news_agent` (headline list + synthesis appended after stream — would appear broken).

### Database

SQLite with WAL mode at `DB_PATH` (default: `../database/aether.db`, Railway: `/data/aether.db`). Uses synchronous `sqlite3` (not async). Two main tables: `tasks` + `steps`. Preferences stored in `preferences` key-value table. `PRAGMA foreign_keys=ON` on every connection.

---

## Environment Variables

| Variable | Notes |
|----------|-------|
| `QWEN_API_KEY` | Required — DashScope key |
| `AETHER_API_KEY` | Recommended in prod — checked via `X-Api-Key` header |
| `DB_PATH` | Set to `/data/aether.db` on Railway (persistent volume) |
| `QWEN_MODEL` | Default: `qwen-turbo-latest` |
| `QWEN_VISION_MODEL` | Default: same as `QWEN_MODEL` |
| `TTS_VOICE` | Default: `en-US-GuyNeural` |
| `SERPER_API_KEY` | Browser agent — Google search via Serper.dev (2500/month free) |
| `GNEWS_API_KEY` | News agent — 100 req/day free |
| `ALPHAVANTAGE_API_KEY` | Finance agent — stocks, 25 req/day free |
| `TASK_RETENTION_DAYS` | Default: `30` |
| `MAX_CONCURRENT_TASKS` | Default: `5` |

`DEEPGRAM_API_KEY` lives in `voice_config.h` on the ESP32 only — never sent to the cloud.

Copy `.env.example` → `.env` for local dev. Never commit `.env`, `voice_config.h`, or `aether_config.ini` with real values.

---

## Design Decisions

- **Qwen via DashScope** — lower cost, same OpenAI-compatible API format
- **Deepgram Nova-3 on ESP32** — persistent WSS for always-on ASR; no WAV upload, no round-trip STT latency
- **Mono mic (channels=1)** — INMP441 is mono; stereo config wasted 50% of the I2S buffer. `MIC_GAIN_SHIFT=12` gives 16× amplification.
- **edge-tts for TTS** — free Microsoft neural voices, direct MP3 output, no API key
- **PSRAM prebuffer for TTS** — all decoded PCM accumulated in PSRAM before playback starts, avoiding ring-buffer races between producer/consumer
- **Sprite canvas for face** — `GFXcanvas16` (320×160 px, ~100KB PSRAM) blit atomically to TFT; eliminates flicker from partial screen updates
- **`bronny_answer()` vs `answer()`** — voice responses use a separate system prompt with Bronny persona so the device never says "I'm AetherAI"
- **`bootup_intro` trigger** — firmware sends this string on first Deepgram connect; cloud brain skips classify/plan and returns a Bronny self-introduction directly
- **Single `index.html`** — no build pipeline; inline CSS/JS; KaTeX loaded from CDN
- **Voice responses capped at ~600 chars** — `_TTS_MAX_CHARS = 600` in `tts_client.py` keeps audio playback under ~10s on the ESP32
- **`pyautogui.FAILSAFE=True`** on device agent — move mouse to top-left to abort automation
- **`STREAM_DATA_GAP_MS=500`** on ESP32 — gap between TCP packets before declaring stream complete; 300ms caused false early exits on congested networks

---

## Bronny AI v1.1 — Key Implementation Details

### Face State Machine

`FaceState` enum: `FS_IDLE`, `FS_TALKING`, `FS_LISTEN`, `FS_THINK`, `FS_HAPPY`, `FS_SURPRISED`, `FS_SLEEP`

- All transitions go through named setters: `setFaceIdle()`, `setFaceTalk()`, `setFaceListen()`, `setFaceSurprised()`, etc.
- `animFace()` is called every 16ms; sets `faceRedraw = true` when geometry changes.
- `drawFace(bool full)` renders to `faceCanvas`, then calls `blitFace()` which blits the 320×160 sprite to TFT at `faceBlitY`.

### Log Visibility

`logsVisible` (default `false` at boot — face centred in full 240px height):

- `true`:  `faceCY=72`, `faceBlitY=0` → face in top 160px, log zone below
- `false`: `faceCY=80`, `faceBlitY=40` → face canvas shifted down 40px, visually centred

Toggle via `setLogsVisible(bool)`, triggered by voice commands or Serial `l` key.

### Deepgram Integration

- Persistent WSS to `api.deepgram.com:443`, path `DG_PATH` includes `model=nova-3`, `channels=1`, `endpointing=350`, `interim_results=true`
- `parseDgMsg()` handles `Results` (interim + final) and `Error` types
- `speech_final=true` sets `pendingTranscript=true` and clears `dgFinalReceivedAt`
- Timeout fallback in `loop()`: if `dgFinalReceivedAt > 0` and `> DG_FINAL_TIMEOUT_MS` (700ms), self-trigger
- `KeepAlive` JSON sent every `DG_KEEPALIVE_MS` (8s) when not streaming audio
- Reconnect attempted every `DG_RECONNECT_MS` (3s) when disconnected, but **not** while `busy`

### Audio Buffers (ESP32)

```cpp
static int32_t s_rawBuf[1600];  // mono 32-bit samples
static int16_t s_pcmBuf[1600];  // after inmp441Sample() shift
```

`inmp441Sample(raw)` = `(int16_t)(raw >> MIC_GAIN_SHIFT)` with clamp to ±32767.

---

## Common Pitfalls

- **Double-render on streaming agents**: do not add an agent to `STREAMING_AGENTS` if it returns structured/tagged output or builds content in two passes
- **MemoryAgent index key**: uses `"pref:@@index@@"` — the `@@` prefix cannot be produced by `_slug()` so user input can never overwrite the index
- **Automation agent vision cleanup**: on early exit from `_vision_task()`, both `unregister_pending()` AND `_vision_handlers.pop()` must be called — `unregister_pending` only clears `_pending` and `_pending_ts`
- **Office COM open**: uses `asyncio.get_running_loop().run_in_executor()` — never `get_event_loop()` (deprecated in Python 3.10+, raises RuntimeError in 3.12)
- **CoinGecko 429**: `_coingecko_get()` retries up to `RETRY_ON_429=2` times with `RETRY_DELAY_429=2.0s` back-off
- **Document agent image queries**: `_build_image_query()` strips stop-words and picks the 4 longest content tokens; do not pass raw slide titles directly to Wikimedia/loremflickr
- **Bronny reconnect guard**: Deepgram reconnect is gated by `if busy: return` to prevent reconnect attempts during active Railway calls
