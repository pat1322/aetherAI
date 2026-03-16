# ⬡ AetherAI — Personal AI Agent System

> A cloud-hosted AI agent that acts as your digital employee.
> Controls computers, automates tasks, researches topics, browses the web,
> generates documents, remembers your preferences, and now speaks and listens
> through a dedicated ESP32-S3 voice device.

---

## Stage 6 — What's Running

| Component | Status | Description |
|---|---|---|
| Cloud Brain (FastAPI) | ✅ | API Gateway, WebSocket hub, SSE streaming endpoint |
| Orchestrator | ✅ | Streaming chat (token-by-token) + task planning with user context |
| Research Agent | ✅ | Academic-style research — DDG multi-source, structured reports, citations |
| Document Agent | ✅ | PowerPoint / Word / Excel — 6 themes, Wikimedia photos |
| Browser Agent | ✅ | Playwright + trafilatura scraping, yt-dlp YouTube, web search |
| Device Agent | ✅ | PC control via WebSocket — mouse, keyboard, vision loop, pywinauto |
| Automation Agent | ✅ | Single actions, sequences, vision loop, Chrome navigation |
| Memory Agent | ✅ | Save/recall/forget user preferences — persists across sessions |
| Weather Agent | ✅ | Open-Meteo — live weather + 7-day forecast, any city, no API key |
| Crypto Agent | ✅ | CoinGecko — live prices in USD + PHP, top 10, trending |
| News Agent | ✅ | GNews + Hacker News — headlines, topic news, morning briefings |
| Finance Agent | ✅ | ExchangeRate-API (currency) + Alpha Vantage (stocks) |
| **STT Client** | ✅ | **Qwen Paraformer ASR — transcribes audio, reuses QWEN_API_KEY** |
| **TTS Client** | ✅ | **edge-tts — free Microsoft neural voices, returns MP3** |
| **Voice Agent** | ✅ | **Full pipeline: WAV → Paraformer → Qwen → edge-tts → MP3** |
| **ESP32 Voice Device** | ✅ | **ESP32-S3 + INMP441 + ES8311 + ST7789 — always-on voice assistant** |
| Web UI | ✅ | Streaming token rendering, 🎤 mic button, TTS readback toggle |
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

### Step 3: Set environment variables in Railway

**Required:**
```
QWEN_API_KEY      = your_dashscope_key
AETHER_API_KEY    = your_secret_here
QWEN_MODEL        = qwen-plus
QWEN_VISION_MODEL = qwen-vl-plus
DB_PATH           = /data/aether.db
```

**Optional (Stage 6 voice — defaults already work):**
```
TTS_VOICE = en-US-AriaNeural   # any edge-tts voice, see list below
```

**Optional (free API keys — unlock full features):**
```
GNEWS_API_KEY         = get free at gnews.io         (news agent — 100 req/day)
ALPHAVANTAGE_API_KEY  = get free at alphavantage.co  (stock prices — 25 req/day)
```

> No key needed for: Open-Meteo (weather), CoinGecko (crypto), ExchangeRate-API (currency),
> Paraformer ASR (reuses QWEN_API_KEY), edge-tts (free, no key)

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

## ESP32 Voice Agent Setup

The ESP32-S3 device acts as a dedicated always-on voice assistant.
Hold the BOOT button to speak, release to send.

### Hardware wiring

| Component | Pins |
|---|---|
| ES8311 codec (speaker) | PA_EN→48, DOUT→45, DIN→12, WS→13, BCLK→14, MCLK→38, SCL→2, SDA→1 |
| INMP441 mic | VDD→3.3V, GND→GND, L/R→GND, WS→4, SCK→5, SD→6 |
| ST7789 TFT 320×240 | DC→39, CS→47, CLK→41, SDA→40, BLK→42 |
| Trigger button | GPIO0 (built-in BOOT button, active LOW) |

### Arduino IDE settings

```
Board:              ESP32S3 Dev Module
PSRAM:              OPI PSRAM (8MB)    ← required for audio buffers
USB CDC on Boot:    Enabled
```

### Required Arduino libraries

- `pschatzmann/arduino-audio-tools`
- `pschatzmann/arduino-audio-driver`
- `Adafruit ST7789` + `Adafruit GFX Library`

### Flash the firmware

```
1. Copy esp32_voice_agent/voice_config.h.example → esp32_voice_agent/voice_config.h
2. Fill in: WIFI_SSID, WIFI_PASSWORD, AETHER_URL, AETHER_API_KEY
3. Open aether_voice.ino in Arduino IDE
4. Select your board + port, click Upload
```

> `voice_config.h` is in `.gitignore` — your credentials are never committed.

### ESP32 TFT state machine

| State | Display |
|---|---|
| IDLE | Animated cyan hexagon logo + "Hold BOOT to speak" |
| RECORDING | Pulsing red ring + VU meter + countdown timer |
| UPLOADING | Arc progress animation |
| THINKING | Three bouncing dots |
| SPEAKING | Animated waveform bars (synced to playback) |
| ERROR | Red ✕ + message, auto-returns to IDLE after 3 seconds |

### Voice pipeline

```
INMP441 mic
    ↓ (16kHz mono WAV, PSRAM buffer)
POST /voice/chat  (HTTPS to Railway)
    ↓
Qwen Paraformer ASR  →  transcript text
    ↓
classify_command()  →  weather/crypto/finance/news agent  OR  Qwen direct answer
    ↓
_voice_summarize()  →  condenses to 1–3 spoken sentences
    ↓
edge-tts  →  MP3 bytes  (response headers: X-Transcript, X-Response-Text)
    ↓
ESP32 MP3DecoderHelix → ES8311 → speaker
```

### Available TTS voices (selection)

```bash
# List all:
python -m edge_tts --list-voices

# Good defaults:
en-US-AriaNeural      Female, neutral (default)
en-US-GuyNeural       Male, neutral
en-GB-SoniaNeural     Female, British
fil-PH-BlessicaNeural Female, Filipino
fil-PH-AngeloNeural   Male, Filipino
```

Set via `TTS_VOICE` env var in Railway.

---

## Web UI — Stage 6 Features

### Streaming responses
Chat answers now stream token-by-token with a blinking cursor.
No more waiting for the full response — you see it as it's generated.

### Voice input (browser)
Click the 🎤 button or press `Ctrl+M` to speak your command.
Uses the Web Speech API — works in Chrome and Edge.
Settings: Voice Readback toggle, Mic Auto-Send toggle, TTS speed (0.75×–2×).

### Keyboard shortcuts
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

### Save
```
remember that my name is Patrick
my timezone is Asia/Manila
i prefer Python over JavaScript
my preferred presentation theme is Ocean Deep
```

### Recall
```
what do you know about me
show my preferences
what is my timezone
```

### Forget
```
forget my timezone
clear all preferences
```

---

## Agent Capabilities

### 💬 Chat (streaming)
General questions, creative writing, math, translations — streamed token-by-token.

### 🔍 Research Agent
Triggered by the word **"research"**. Searches the web and produces structured
academic-style reports with real URL citations.

### 🌐 Browser Agent
Scrapes URLs, searches YouTube, reads Hacker News, does web searches.

### 📄 Document Agent
Creates downloadable PowerPoint, Word, and Excel files.

### 💻 Coding Agent
Writes and saves code files (Python, JS, C, C++, Java, Rust, Go, and more).

### 🌤️ Weather Agent
Live weather + 7-day forecast via Open-Meteo. No API key needed.

### 🪙 Crypto Agent
Live prices in USD and PHP via CoinGecko. No API key needed.

### 📰 News Agent
Headlines and briefings via GNews + Hacker News fallback.

### 💱 Finance Agent
Currency conversion + stock prices (Alpha Vantage).

### 🎙️ Voice Agent (ESP32 + Web)
Speak commands to the ESP32 — it transcribes, thinks, and speaks back.
Also available in the browser via the 🎤 mic button.

### 🖥️ Automation Agent
Controls your physical PC — open apps, type text, navigate Chrome, take screenshots.

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| GET | `/` | System info |
| GET | `/health` | Health check + task stats |
| GET | `/ui/config` | Auto-fetch API key for web UI (public) |
| POST | `/command` | Send a command (WebSocket streaming response) |
| POST | `/stream` | Send a command (SSE streaming response) |
| POST | `/voice/chat` | ESP32 voice endpoint — WAV in, MP3 out |
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
| WS | `/ws/device/{id}` | Device Agent connection |
| WS | `/ws/ui/{id}` | Web UI live updates |

---

## Project Structure

```
aetherAI/
├── cloud_brain/
│   ├── main.py                        # FastAPI app, endpoints, SSE, voice
│   ├── orchestrator.py                # Streaming chat + task planning
│   ├── agent_router.py                # Routes to correct agent
│   ├── memory.py                      # SQLite — WAL, FK, auto-cleanup
│   ├── config.py                      # Settings — includes TTS_VOICE
│   ├── agents/
│   │   ├── __init__.py                # BaseAgent
│   │   ├── research_agent.py          # Academic research, trafilatura
│   │   ├── document_agent.py          # PPTX/DOCX/XLSX, 6 themes
│   │   ├── browser_agent.py           # Playwright + trafilatura + yt-dlp
│   │   ├── coding_agent.py            # Code generation, syntax validation
│   │   ├── automation_agent.py        # PC control, vision loop
│   │   ├── memory_agent.py            # Preferences CRUD
│   │   ├── weather_agent.py           # Open-Meteo
│   │   ├── crypto_agent.py            # CoinGecko
│   │   ├── news_agent.py              # GNews + Hacker News
│   │   ├── finance_agent.py           # ExchangeRate-API + Alpha Vantage
│   │   └── voice_agent.py             # STT → LLM → TTS pipeline ← Stage 6
│   └── utils/
│       ├── qwen_client.py             # Qwen API — blocking + streaming
│       ├── websocket_manager.py       # WS manager, queues, stream chunks
│       ├── stt_client.py              # Paraformer ASR wrapper ← Stage 6
│       └── tts_client.py              # edge-tts synthesis wrapper ← Stage 6
├── device_agent/
│   ├── agent.py                       # PC control, pywinauto, vision loop
│   ├── config.py
│   ├── aether_config.ini
│   ├── build_exe.bat
│   └── requirements.txt
├── esp32_voice_agent/                 # ← Stage 6
│   ├── aether_voice.ino               # Full ESP32 voice agent firmware
│   └── voice_config.h.example        # WiFi + URL config template
├── web_ui/
│   └── index.html                     # Streaming UI, mic button, TTS toggle
├── database/
├── output/
├── nixpacks.toml
├── requirements.txt
├── Procfile
├── .env.example
└── .gitignore
```

---

## Environment Variables Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `QWEN_API_KEY` | ✅ Yes | — | DashScope API key (also used for Paraformer ASR) |
| `QWEN_MODEL` | No | `qwen-turbo` | Text model |
| `QWEN_VISION_MODEL` | No | same as QWEN_MODEL | Vision model for screenshot analysis |
| `QWEN_BASE_URL` | No | DashScope intl | API base URL |
| `AETHER_API_KEY` | Recommended | — | Protects API endpoints |
| `DB_PATH` | No | `../database/aether.db` | SQLite path — use `/data/aether.db` on Railway |
| `TTS_VOICE` | No | `en-US-AriaNeural` | edge-tts voice for ESP32 + browser readback |
| `GNEWS_API_KEY` | No | — | GNews headlines (100 req/day free) |
| `ALPHAVANTAGE_API_KEY` | No | — | Stock prices (25 req/day free) |
| `TASK_RETENTION_DAYS` | No | `30` | Auto-purge old tasks |
| `MAX_CONCURRENT_TASKS` | No | `5` | Max parallel tasks before 429 |

---

## Roadmap

- **Stage 1** ✅ Cloud Brain + API + Orchestrator + Research Agent
- **Stage 2** ✅ Document Agent (PPTX, DOCX, XLSX — 6 themes)
- **Stage 3** ✅ Device Agent (vision loop, mouse/keyboard, app automation)
- **Stage 4** ✅ Browser Agent (Playwright, YouTube, scraping) + Standalone EXE
- **Stage 5** ✅ Memory system + Weather/Crypto/News/Finance agents + trafilatura + yt-dlp + pywinauto
- **Stage 6** ✅ Streaming responses + Browser mic + ESP32-S3 voice agent (INMP441 + ES8311 + ST7789)
- **Stage 7** — Web Dashboard v2 (multi-device management, scheduled tasks)

---

## Safety Notes

- Set `AETHER_API_KEY` in production — prevents unauthorized API access
- The web UI fetches the key automatically — users never need to enter it
- `voice_config.h` is gitignored — WiFi credentials never leave your machine
- Device Agent has pyautogui FAILSAFE enabled (move mouse to top-left to abort)
- Preferences are stored in your Railway SQLite volume — never leave your instance
- Each connected device shows up by `device_id` — give each person a unique ID

---

## Letting Others Connect Their PC

### Build the EXE (once)
```bash
cd device_agent
build_exe.bat
```

### Distribute
Send these two files:
```
AetherAI_Agent.exe
aether_config.ini
```

They edit `aether_config.ini`:
```ini
[aether]
cloud_url = wss://aetherai.up.railway.app
device_id = johns-laptop
api_key   = your_shared_api_key
```

Double-click `AetherAI_Agent.exe` to connect.
