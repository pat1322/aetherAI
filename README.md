# ⬡ AetherAI — Personal AI Agent System

> A cloud-hosted AI agent platform that controls computers, automates tasks,
> researches topics, browses the web, generates documents, remembers your
> preferences, and speaks and listens through a dedicated ESP32-S3 voice device.

**Stage 7 — Cloud Brain v7.0 · Bronny AI v1.1**

---

## What's Running

| Component | Status | Description |
|---|---|---|
| Cloud Brain (FastAPI) | ✅ | API Gateway, WebSocket hub, SSE streaming endpoint |
| Orchestrator | ✅ | Token-by-token streaming chat + multi-step task planning with user context |
| Research Agent | ✅ | Academic-style research — DDG multi-source, structured reports, citations |
| Document Agent | ✅ | PowerPoint / Word / Excel — 6 themes, Wikimedia + loremflickr photos |
| Browser Agent | ✅ | Playwright + trafilatura scraping, yt-dlp YouTube, Serper/SearXNG/DDG web search |
| Device Agent | ✅ | PC control via WebSocket — mouse, keyboard, vision loop, pywinauto |
| Automation Agent | ✅ | Single actions, sequences, vision loop, Chrome navigation |
| Memory Agent | ✅ | Save/recall/forget user preferences — persists across sessions |
| Weather Agent | ✅ | Open-Meteo + wttr.in fallback — live weather + 7-day forecast, any city, no API key |
| Crypto Agent | ✅ | CoinGecko — live prices in USD + PHP, top 10, trending, 429 retry back-off |
| News Agent | ✅ | GNews + Hacker News — headlines, topic news, morning briefings |
| Finance Agent | ✅ | ExchangeRate-API (currency) + Alpha Vantage (stocks) |
| TTS Client | ✅ | edge-tts — free Microsoft neural voices, returns MP3 |
| Voice Agent | ✅ | text → Qwen LLM → edge-tts → MP3 pipeline |
| **Bronny AI v1.1** | ✅ | **ESP32-S3 · Deepgram Nova-3 WSS ASR · ES8311 DAC · ST7789 Sprite Edition** |
| Web UI | ✅ | Token streaming, 🎤 mic button, TTS readback, KaTeX math rendering |
| Memory (SQLite) | ✅ | WAL mode, FK constraints, indexes, auto-cleanup, Railway volume |
| Standalone EXE | ✅ | Others can connect their PC without installing Python |

---

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/YOUR_USERNAME/aetherAI.git
cd aetherAI
cp .env.example .env
# Edit .env — add your QWEN_API_KEY and optional keys
```

### 2. Run the Cloud Brain locally

```bash
pip install -r requirements.txt
playwright install chromium
cd cloud_brain
uvicorn main:app --reload --port 8000
```

Open: http://localhost:8000/ui/index.html

### 3. Run the Device Agent on your PC

```bash
cd device_agent
pip install -r requirements.txt
python agent.py
```

---

## Deploy to Railway

### Step 1: Push to GitHub

```bash
git add .
git commit -m "Deploy"
git push origin main
```

### Step 2: Create Railway project

1. Go to [railway.app](https://railway.app)
2. New Project → Deploy from GitHub repo → select `aetherAI`
3. Railway auto-detects `nixpacks.toml` (installs Playwright at build time)

### Step 3: Set environment variables

**Required:**
```
QWEN_API_KEY      = your_dashscope_key
AETHER_API_KEY    = your_secret_here
QWEN_MODEL        = qwen-plus
QWEN_VISION_MODEL = qwen-vl-plus
DB_PATH           = /data/aether.db
```

**Optional (free API keys — unlock full features):**
```
TTS_VOICE             = en-US-AriaNeural   # any edge-tts voice
GNEWS_API_KEY         = gnews.io           # news agent — 100 req/day free
ALPHAVANTAGE_API_KEY  = alphavantage.co    # stock prices — 25 req/day free
SERPER_API_KEY        = serper.dev         # Google search — 2500 req/month free
```

> No key needed for: Open-Meteo (weather), CoinGecko (crypto), ExchangeRate-API
> (currency), Hacker News, edge-tts

### Step 4: Add persistent volume

1. Railway project → your service → **Volumes** tab
2. New Volume → mount path: `/data`
3. Set env var: `DB_PATH = /data/aether.db`

### Step 5: Connect Device Agent to Railway

Edit `device_agent/aether_config.ini`:
```ini
[aether]
cloud_url = wss://aetherai.up.railway.app
device_id = patrick-pc
api_key   = your_secret_here
```

---

## ESP32 Voice Agent — Bronny AI v1.1

A dedicated always-on hands-free voice assistant. Deepgram streams audio
continuously and triggers responses via VAD — no button press required.

### Hardware Wiring

| Component | Pins |
|---|---|
| ES8311 codec (speaker) | PA_EN→48, DOUT→45, DIN→12, WS→13, BCLK→14, MCLK→38, SCL→2, SDA→1 |
| INMP441 mic (mono) | VDD→3.3V, GND→GND, L/R→GND, WS→4, SCK→5, SD→6 |
| ST7789 TFT 320×240 | DC→39, CS→47, CLK→41, SDA→40, BLK→42 |

### Arduino IDE Settings

```
Board:              ESP32S3 Dev Module
PSRAM:              OPI PSRAM (8MB)    ← required for sprite canvas + audio buffers
USB CDC on Boot:    Enabled
```

### Required Arduino Libraries

- `pschatzmann/arduino-audio-tools`
- `pschatzmann/arduino-audio-driver`
- `Links2004/arduinoWebSockets` (WebSocketsClient — for Deepgram)
- `bblanchon/ArduinoJson`
- `Adafruit ST7789` + `Adafruit GFX Library`

### Flash the Firmware

```
1. Copy esp32_voice_agent/voice_config.h.example → voice_config.h
2. Fill in: WIFI_SSID, WIFI_PASS, AETHER_URL, AETHER_API_KEY, DEEPGRAM_API_KEY
3. Open bronny_ai.ino in Arduino IDE
4. Select your board + port, click Upload
```

> `voice_config.h` is in `.gitignore` — credentials are never committed.

> Get a free Deepgram API key at [console.deepgram.com](https://console.deepgram.com).

### Voice Pipeline

```
INMP441 mic  (16kHz mono, 32-bit I2S, channels=1)
    ↓
Deepgram Nova-3 (persistent WSS to api.deepgram.com)
    → interim_results + endpointing=350ms VAD
    → speech_final event fires → transcript ready
    ↓
isNoise() filter → discard if noise
    ↓
POST /voice/text  (pre-transcribed text → Railway)
    ↓
classify_command() → agent or Qwen direct answer
    ↓
_voice_summarize() → condenses to 1–3 spoken sentences
    ↓
edge-tts → MP3 bytes streamed back
    ↓
MP3DecoderHelix → ES8311 codec → speaker
    ↓
Deepgram streaming resumes → back to listening
```

### Mic Configuration

The INMP441 is a mono microphone. The firmware reads a single channel (`channels=1`).
Gain is controlled by `MIC_GAIN_SHIFT` (default 12 = 16× gain). Increase to 10 for
louder environments; decrease to 14 if audio clips.

### Face States (Sprite Edition)

The face renders to a `GFXcanvas16` sprite (320×160 px, ~100KB PSRAM) and blits
atomically to TFT — no flicker.

| State | Display |
|---|---|
| `FS_IDLE` | Animated robot face, idle bob, random eye glances, blinking |
| `FS_LISTEN` | Listening pulse, eyes alert — active while Deepgram streams |
| `FS_THINK` | Eyes squinted, looking up-right — while Railway is processing |
| `FS_TALKING` | Mouth animates open/close synced to speech playback |
| `FS_HAPPY` | Wide smile, happy bounce — shown after a successful response |
| `FS_SURPRISED` | Eyes enlarged, open mouth — shown on wake-word detection |
| `FS_SLEEP` | Eyes closed, ZZZ particle animation — standby mode |

### Standby & Wake Word

After **3 minutes** of no Railway calls, Bronny enters standby (sleep face + ZZZ).
Say any of the following to wake: **"bronny"**, **"hi bronny"**, **"hey bronny"**,
**"brownie"**, **"brawny"**, or several phonetic variants.

### Log Visibility Toggle

At boot, logs are **hidden** (face centred on the full 240px screen). Toggle at any time:

| Method | Effect |
|---|---|
| Say "show logs" / "display logs" | Shows log zone below face |
| Say "hide logs" / "hide the logs" | Returns to face-only mode |
| Serial Monitor: press `l` | Toggles manually |
| Serial Monitor: press `m` | Prints Deepgram connection status |

### Bronny Identity

All voice responses use `bronny_answer()` on the cloud brain so Bronny never
identifies as AetherAI. The boot intro (`bootup_intro` trigger) delivers a
warm self-introduction on first power-on after Deepgram connects.

---

## Web UI Features

### Streaming Responses
Chat answers stream token-by-token with a blinking cursor. Research and browser
agent results also stream as they are written. No waiting for full responses.

### Voice Input (Browser)
Click 🎤 or press `Ctrl+M` to speak. Uses the Web Speech API (Chrome/Edge).

Settings: Voice Readback toggle, Mic Auto-Send toggle, TTS speed (0.75×–2×).
Readback auto-detects Filipino text and routes to a Filipino voice if available.

### Math Rendering
KaTeX renders inline (`$...$`) and display (`$$...$$`) math expressions.

### Keyboard Shortcuts

| Key | Action |
|---|---|
| `Enter` | Send command |
| `↑ / ↓` | Navigate command history |
| `Ctrl+M` | Toggle microphone |
| `Ctrl+L` | Clear output |
| `Escape` | Clear input / stop mic |

---

## Memory System

AetherAI remembers facts and preferences across all sessions.

```
remember that my name is Patrick
my timezone is Asia/Manila
i prefer Python over JavaScript
my preferred presentation theme is Ocean Deep
```

Recall: `what do you know about me` · Forget: `forget my timezone`

---

## Web Search Priority (Browser Agent)

1. **Serper.dev** — Google results via API, free 2500/month (`SERPER_API_KEY`)
2. **SearXNG** — meta-search (Google + Bing + DDG), public instances, no key
3. **Playwright + Google** — headless real Google (if Playwright installed)
4. **DDG Lite** — HTML scrape fallback
5. **Knowledge fallback** — model answer with disclaimer

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| GET | `/` | System info |
| GET | `/health` | Health check + task stats |
| GET | `/ui/config` | Auto-fetch API key for web UI (public) |
| POST | `/command` | Send a command (WebSocket streaming response) |
| POST | `/stream` | Send a command (SSE streaming response) |
| GET | `/tts/voices` | List all available edge-tts voices |
| GET | `/task/{id}` | Get task status |
| GET | `/tasks` | List recent tasks |
| POST | `/task/{id}/cancel` | Cancel a task |
| DELETE | `/task/{id}` | Delete a task |
| DELETE | `/tasks/all` | Delete all tasks |
| GET | `/files` | List generated files |
| GET | `/files/download/{filename}` | Download a file (public) |
| DELETE | `/files/{filename}` | Delete a file |
| DELETE | `/files/all/clear` | Delete all files |
| GET | `/preferences` | List all preferences |
| POST | `/preferences` | Save a preference |
| DELETE | `/preferences/all` | Clear all preferences |
| DELETE | `/preferences/{key}` | Delete one preference |
| GET | `/devices` | List connected devices |
| POST | `/bronny/heartbeat` | Bronny device keepalive |
| GET | `/bronny/status` | Bronny online/offline status |
| POST | `/voice/text` | Pre-transcribed text → LLM → TTS → MP3 |
| WS | `/ws/device/{id}` | Device Agent connection |
| WS | `/ws/ui/{id}` | Web UI live updates |

---

## Project Structure

```
aetherAI/
├── cloud_brain/
│   ├── main.py                     # FastAPI app, all endpoints, voice pipeline
│   ├── orchestrator.py             # Streaming chat + task planning
│   ├── agent_router.py             # Routes to correct agent
│   ├── memory.py                   # SQLite — WAL, FK, auto-cleanup
│   ├── config.py                   # Settings (TTS_VOICE, DB_PATH, etc.)
│   ├── agents/
│   │   ├── __init__.py             # BaseAgent + stream_llm / stream_summarize
│   │   ├── research_agent.py       # DDG search, page fetch, streaming reports
│   │   ├── document_agent.py       # PPTX/DOCX/XLSX, 6 themes, Wikimedia images
│   │   ├── browser_agent.py        # Playwright + trafilatura + yt-dlp
│   │   ├── coding_agent.py         # Code generation, syntax validation
│   │   ├── automation_agent.py     # PC control, vision loop
│   │   ├── memory_agent.py         # Preferences CRUD
│   │   ├── weather_agent.py        # Open-Meteo + wttr.in fallback
│   │   ├── crypto_agent.py         # CoinGecko + 429 retry
│   │   ├── news_agent.py           # GNews + Hacker News
│   │   ├── finance_agent.py        # ExchangeRate-API + Alpha Vantage
│   │   └── voice_agent.py          # _voice_summarize + _safe_synthesize
│   └── utils/
│       ├── qwen_client.py          # Qwen API — blocking + streaming + bronny_answer
│       ├── websocket_manager.py    # WS pool, session routing, stream chunks
│       └── tts_client.py           # edge-tts synthesis wrapper
├── device_agent/
│   ├── agent.py                    # PC control — pyautogui, pywinauto, vision loop
│   ├── config.py                   # Reads from aether_config.ini or env vars
│   ├── aether_config.ini           # Local credentials (never commit with filled values)
│   ├── build_exe.bat               # PyInstaller build script
│   └── requirements.txt
├── esp32_voice_agent/
│   ├── bronny_ai.ino               # Bronny AI v1.1 firmware
│   └── voice_config.h.example     # WiFi + credentials template (copy → voice_config.h)
├── web_ui/
│   └── index.html                  # Streaming UI, mic, TTS, KaTeX — no build step
├── database/                       # Local SQLite (gitignored)
├── output/                         # Generated files (gitignored)
├── nixpacks.toml                   # Railway build config (Playwright at build time)
├── requirements.txt
├── Procfile
├── .env.example
└── .gitignore
```

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `QWEN_API_KEY` | ✅ | — | DashScope API key |
| `QWEN_MODEL` | No | `qwen-turbo-latest` | Text LLM model |
| `QWEN_VISION_MODEL` | No | same as QWEN_MODEL | Vision model for screenshot analysis |
| `QWEN_BASE_URL` | No | DashScope intl | API base URL |
| `AETHER_API_KEY` | Recommended | — | Protects API endpoints via `X-Api-Key` header |
| `DB_PATH` | No | `../database/aether.db` | SQLite path — use `/data/aether.db` on Railway |
| `TTS_VOICE` | No | `en-US-GuyNeural` | edge-tts voice for ESP32 + browser readback |
| `SERPER_API_KEY` | No | — | Google search (2500 req/month free) |
| `GNEWS_API_KEY` | No | — | GNews headlines (100 req/day free) |
| `ALPHAVANTAGE_API_KEY` | No | — | Stock prices (25 req/day free) |
| `TASK_RETENTION_DAYS` | No | `30` | Auto-purge old tasks |
| `MAX_CONCURRENT_TASKS` | No | `5` | Max parallel tasks before 429 |

`DEEPGRAM_API_KEY` lives in `voice_config.h` on the ESP32 only — it is never sent to the cloud brain.

---

## Distributing the Device Agent

### Build the EXE (once)
```bash
cd device_agent
build_exe.bat
```

### Distribute
Send two files: `AetherAI_Agent.exe` + `aether_config.ini`

```ini
[aether]
cloud_url = wss://aetherai.up.railway.app
device_id = johns-laptop
api_key   = your_shared_api_key
```

---

## Safety Notes

- Set `AETHER_API_KEY` in production — prevents unauthorized API access
- The web UI fetches the key automatically from `/ui/config` — users never type it
- `voice_config.h` is gitignored — WiFi, AetherAI, and Deepgram credentials stay local
- `aether_config.ini` has no real key in the repo — fill it in locally only
- Device Agent has `pyautogui.FAILSAFE = True` — move mouse to top-left to abort
- Preferences are stored in your Railway SQLite volume — never leave your instance

---

## Roadmap

- **Stage 1** ✅ Cloud Brain + API + Orchestrator + Research Agent
- **Stage 2** ✅ Document Agent (PPTX, DOCX, XLSX — 6 themes)
- **Stage 3** ✅ Device Agent (vision loop, mouse/keyboard, app automation)
- **Stage 4** ✅ Browser Agent (Playwright, YouTube, scraping) + Standalone EXE
- **Stage 5** ✅ Memory + Weather/Crypto/News/Finance + trafilatura + yt-dlp + pywinauto
- **Stage 6** ✅ Streaming responses + browser mic + ESP32-S3 voice agent
- **Stage 7** ✅ Bronny AI v1.1 — Deepgram Nova-3 WSS ASR, always-on hands-free, sprite animation engine, standby/wake-word, log visibility voice commands, mono mic, Bronny identity patch

---

## License

MIT License — see [LICENSE](LICENSE) for details.  
© 2026 Patrick Perez
