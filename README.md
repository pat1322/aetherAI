# ⬡ AetherAI — Personal AI Agent System

> A cloud-hosted AI agent that acts as your digital employee.
> Controls computers, automates tasks, researches topics, browses the web, generates documents, and remembers your preferences.

---

## Stage 5 — What's Running

| Component | Status | Description |
|---|---|---|
| Cloud Brain (FastAPI) | ✅ | API Gateway, WebSocket hub, lifespan handler |
| Orchestrator | ✅ | Plans tasks with Qwen, injects user preferences into every call |
| Research Agent | ✅ | Academic-style research — DDG multi-source, structured reports, real URL citations |
| Document Agent | ✅ | PowerPoint (Picsum/Wikimedia photos, 4-layout engine), Word, Excel — 6 themes |
| Browser Agent | ✅ | Playwright + trafilatura scraping, yt-dlp YouTube, web search, Hacker News |
| Device Agent | ✅ | PC control via WebSocket — mouse, keyboard, vision loop, pywinauto file ops |
| Automation Agent | ✅ | Single actions, sequences, vision loop, Chrome navigation, calculator input |
| Memory Agent | ✅ | Save/recall/forget user preferences — persists across sessions |
| **Weather Agent** | ✅ | **Open-Meteo — live weather + 7-day forecast, any city, no API key** |
| **Crypto Agent** | ✅ | **CoinGecko — live prices in USD + PHP, top 10, trending** |
| **News Agent** | ✅ | **GNews + Hacker News — headlines, topic news, morning briefings** |
| **Finance Agent** | ✅ | **ExchangeRate-API (currency) + Alpha Vantage (stocks)** |
| Web UI | ✅ | Command center — auto auth, device display, task history, dark/light mode |
| Memory (SQLite) | ✅ | WAL mode, FK constraints, indexes, auto-cleanup, Railway volume support |
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
3. Railway auto-detects the `nixpacks.toml` (installs Playwright at build time)

### Step 3: Set environment variables in Railway

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
GNEWS_API_KEY         = get free at gnews.io         (news agent — 100 req/day)
ALPHAVANTAGE_API_KEY  = get free at alphavantage.co  (stock prices — 25 req/day)
```

> No key needed for: Open-Meteo (weather), CoinGecko (crypto), ExchangeRate-API (currency)

### Step 4: Add persistent volume (keeps memory across deploys)

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

Then run:
```bash
cd device_agent
python agent.py
```

---

## Memory System

AetherAI remembers facts and preferences about you across all sessions.

### Save
```
remember that my name is Patrick
my timezone is Asia/Manila
i prefer Python over JavaScript
note that I use VS Code as my editor
my preferred presentation theme is Ocean Deep
```

### Recall
```
what do you know about me
show my preferences
what is my timezone
do you remember my name
```

### Forget
```
forget my timezone
delete my name preference
clear all preferences
```

---

## Agent Capabilities

### 💬 Chat (Direct Qwen)
For general questions, creative writing, math, translations, explanations.
```
what is quantum computing
write me a haiku about rain
translate "good morning" to Japanese
what are the health benefits of green tea
who is Linus Torvalds
```

### 🔍 Research Agent
Triggered by the word **"research"**. Searches the web, fetches real pages using trafilatura, and produces structured academic-style reports with URL citations.
```
research the latest developments in AI 2025
research raspberry pi 5 specs
research the history of the Philippines
```

### 🌐 Browser Agent
For scraping specific URLs, YouTube searches, and real-time web data.
```
go to wikipedia.org/wiki/Manila and summarize it
search youtube for lofi hip hop music
go to https://news.ycombinator.com and summarize top stories
search google for best Python libraries
```

### 📄 Document Agent
Creates downloadable files.
```
create a presentation about climate change
create a word document about deforestation
create a spreadsheet tracking monthly expenses
```

### 💻 Coding Agent
Writes and saves code files.
```
write a python program that sorts a list of numbers
write a javascript function to check if a string is a palindrome
write a C program that calculates fibonacci numbers
```

### 🌤️ Weather Agent
Live weather and forecasts via Open-Meteo (no API key needed).
```
what's the weather in Manila
weather forecast for Cebu this week
will it rain in Davao tomorrow
temperature in Tokyo today
```

### 🪙 Crypto Agent
Live cryptocurrency prices via CoinGecko.
```
what is the price of bitcoin in PHP
top 10 cryptocurrencies
ethereum price
trending coins
```

### 📰 News Agent
Headlines and briefings via GNews + Hacker News fallback.
```
give me today's tech news
morning briefing
what's happening in the Philippines
business news today
news about AI
```

### 💱 Finance Agent
Currency conversion and stock prices.
```
convert 1000 USD to PHP
exchange rate USD to EUR
Apple stock price
TSLA stock today
how much is 1 dollar in pesos
```

### 🖥️ Automation Agent (requires Device Agent)
Controls your physical PC.

#### Open apps
```
open notepad
open calculator
open chrome
open word and write a letter to my friend
open excel
open powerpoint
```

#### Navigate Chrome
```
open chrome and go to youtube
open chrome and search for AI news
open chrome and go to reddit
go to youtube
go to github
```

#### Calculator math
```
open calculator and calculate 25 * 4
calculate 1500 / 12
compute 15% of 3000
```

#### File operations
```
open my Downloads folder
open my Documents folder
list files in my Documents
find the file called budget.xlsx and open it
open file explorer
```

#### Screenshots
```
take a screenshot
capture my screen
```

#### System commands
```
run the command ipconfig
run the command dir
```

---

## Memory (Context Injection)

Every Qwen call automatically includes your stored preferences — Qwen knows your name, timezone, preferred language, etc. without you repeating them.

Flow:
1. User command → Orchestrator loads preferences from SQLite
2. Preferences injected into Qwen system prompt
3. Qwen plans/answers with your context already in mind

---

## Browser Agent — YouTube

YouTube search now uses `yt-dlp` (if installed) for real video metadata, falling back to DuckDuckGo `site:youtube.com` search. Results include video descriptions and a summary of what the search found.

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

### Auto-start on Windows boot
1. `Win + R` → `shell:startup` → Enter
2. Drop a shortcut to `AetherAI_Agent.exe` in that folder

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| GET | `/` | System info |
| GET | `/health` | Health check + task stats |
| GET | `/ui/config` | Auto-fetch API key for web UI (public) |
| POST | `/command` | Send a command |
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
│   ├── main.py                        # FastAPI app, lifespan, endpoints
│   ├── orchestrator.py                # Task planning, user context injection
│   ├── agent_router.py                # Routes to correct agent
│   ├── memory.py                      # SQLite — WAL, FK, auto-cleanup
│   ├── config.py                      # Settings from env vars
│   ├── agents/
│   │   ├── __init__.py                # BaseAgent
│   │   ├── research_agent.py          # Academic research, trafilatura, citations
│   │   ├── document_agent.py          # PPTX/DOCX/XLSX, 6 themes, Wikimedia photos
│   │   ├── browser_agent.py           # Playwright + trafilatura + yt-dlp
│   │   ├── coding_agent.py            # Code generation, syntax validation
│   │   ├── automation_agent.py        # PC control, vision loop
│   │   ├── memory_agent.py            # Preferences CRUD
│   │   ├── weather_agent.py           # Open-Meteo weather + forecasts
│   │   ├── crypto_agent.py            # CoinGecko prices
│   │   ├── news_agent.py              # GNews + Hacker News
│   │   └── finance_agent.py           # ExchangeRate-API + Alpha Vantage
│   └── utils/
│       ├── qwen_client.py             # Qwen API, routing, planning
│       └── websocket_manager.py       # WS manager, queues, prune
├── device_agent/
│   ├── agent.py                       # PC control, pywinauto, vision loop
│   ├── config.py
│   ├── aether_config.ini
│   ├── build_exe.bat
│   └── requirements.txt
├── web_ui/
│   └── index.html                     # Command center, auto auth, device display
├── database/
├── output/
├── nixpacks.toml                      # Railway build config (Playwright at build time)
├── requirements.txt
├── Procfile
├── .env.example
└── .gitignore
```

---

## Environment Variables Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `QWEN_API_KEY` | ✅ Yes | — | DashScope API key |
| `QWEN_MODEL` | No | `qwen-turbo` | Text model |
| `QWEN_VISION_MODEL` | No | same as QWEN_MODEL | Vision model for screenshot analysis |
| `QWEN_BASE_URL` | No | DashScope intl | API base URL |
| `AETHER_API_KEY` | Recommended | — | Protects API endpoints |
| `DB_PATH` | No | `../database/aether.db` | SQLite path — use `/data/aether.db` on Railway |
| `GNEWS_API_KEY` | No | — | GNews headlines (100 req/day free) |
| `ALPHAVANTAGE_API_KEY` | No | — | Stock prices (25 req/day free) |
| `TASK_RETENTION_DAYS` | No | `30` | Auto-purge old tasks |

---

## Roadmap

- **Stage 1** ✅ Cloud Brain + API + Orchestrator + Research Agent
- **Stage 2** ✅ Document Agent (PPTX, DOCX, XLSX — 6 themes)
- **Stage 3** ✅ Device Agent (vision loop, mouse/keyboard, app automation)
- **Stage 4** ✅ Browser Agent (Playwright, YouTube, scraping) + Standalone EXE
- **Stage 5** ✅ Memory system + Weather/Crypto/News/Finance agents + trafilatura + yt-dlp + pywinauto
- **Stage 6** — Streaming responses + Voice input (Web Speech API)
- **Stage 7** — Web Dashboard v2 (multi-device management, scheduled tasks)

---

## Safety Notes

- Set `AETHER_API_KEY` in production — this prevents unauthorized API access
- The web UI fetches the key automatically — users never need to enter it
- Device Agent has pyautogui FAILSAFE enabled (move mouse to top-left to abort)
- Preferences are stored in your Railway SQLite volume — never leave your instance
- Each connected device shows up by `device_id` — give each person a unique ID
