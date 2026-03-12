# ⬡ AetherAI — Personal AI Agent System

> A cloud-hosted AI agent that acts as your digital employee.
> Controls computers, automates tasks, researches topics, browses the web, generates documents, and remembers your preferences.

---

## Stage 5 — What's Running

| Component | Status | Description |
|---|---|---|
| Cloud Brain (FastAPI) | ✅ | API Gateway, WebSocket hub |
| Orchestrator | ✅ | Plans tasks with Qwen, injects user preferences into every call |
| Research Agent | ✅ | DuckDuckGo HTML + JSON fallback, source URLs, dedup |
| Document Agent | ✅ | PowerPoint (Picsum photos, 4-layout engine), Word, Excel — 6 themes |
| Browser Agent | ✅ | Playwright — Google, YouTube, scraping, multi-step workflows |
| Device Agent | ✅ | PC control via WebSocket (mouse, keyboard, vision loop) |
| Automation Agent | ✅ | Single actions, sequences, and vision loop execution |
| **Memory Agent** | ✅ | **Stage 5 — save/recall/forget user preferences and personal facts** |
| Web UI | ✅ | Command center dashboard (command history, settings modal, busy bar) |
| Memory (SQLite) | ✅ | Tasks, steps, preferences — WAL mode, indexes, auto-cleanup |
| Standalone EXE | ✅ | Others can connect their PC without installing Python |

---

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/YOUR_USERNAME/aetherAI.git
cd aetherAI
cp .env.example .env
# Edit .env — add your QWEN_API_KEY
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
git commit -m "Stage 5: Memory system"
git push origin main
```

### Step 2: Create Railway project

1. Go to [railway.app](https://railway.app)
2. New Project → Deploy from GitHub repo → select `aetherAI`
3. Railway auto-detects the `Procfile`

### Step 3: Set environment variables in Railway

```
QWEN_API_KEY    = your_key_here
AETHER_API_KEY  = your_secret_here
QWEN_MODEL      = qwen-plus
```

### Step 4: Connect Device Agent to Railway

```bash
export AETHER_CLOUD_URL=https://your-app.railway.app
export AETHER_API_KEY=your_secret_here
python agent.py
```

---

## Memory System (Stage 5)

AetherAI now remembers facts and preferences about you across all sessions.
Tell it something once and it applies that knowledge to every future command automatically.

### Saving preferences

```
"Remember that I prefer Python over JavaScript"
"My Railway URL is https://aetherai.up.railway.app"
"I'm based in Manila, Philippines (Asia/Manila timezone)"
"Note that I use VS Code as my editor"
"My preferred presentation theme is Ocean Deep"
```

### Recalling preferences

```
"What do you know about me?"
"Show my preferences"
"Do you remember my Railway URL?"
"What's my preferred language?"
```

### Deleting preferences

```
"Forget my timezone"
"Delete my language preference"
"Clear all preferences"
```

### How it works

1. Any command matching a memory keyword (remember, recall, forget, "what do you know", "my X is Y", etc.) is hard-routed to the **memory_agent** before Qwen even sees it.
2. The memory agent uses Qwen to extract a structured `label` + `value` from natural language, then stores it in the SQLite `preferences` table.
3. Before **every** command — task or chat — the orchestrator calls `MemoryAgent.load_context()` to build a compact preferences string and injects it into the Qwen system prompt.
4. Result: Qwen always knows your preferences when writing plans and answers, without you repeating yourself.

### Preference REST API

| Method | Endpoint | Description |
|---|---|---|
| GET | `/preferences` | List all stored preferences |
| POST | `/preferences` | Save a preference directly (JSON: `{label, value}`) |
| DELETE | `/preferences/all` | Wipe all preferences |
| DELETE | `/preferences/{key}` | Delete one preference by key |

---

## Letting Others Connect Their PC (Standalone EXE)

Other people can connect their own PC to AetherAI without installing Python.

### Build the EXE (you do this once)

```bash
cd device_agent
build_exe.bat
```

This produces `dist/AetherAI_Agent.exe`.

### Distribute to others

Send them two files:
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

Then they just double-click `AetherAI_Agent.exe`.

### Optional: Auto-start on Windows boot

1. Press `Win + R` → type `shell:startup` → press Enter
2. Drop a shortcut to `AetherAI_Agent.exe` in that folder

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| GET | `/` | System info |
| GET | `/health` | Health check + task stats |
| POST | `/command` | Send a command |
| GET | `/task/{id}` | Get task status |
| GET | `/tasks` | List recent tasks |
| POST | `/task/{id}/cancel` | Cancel a task |
| DELETE | `/task/{id}` | Delete a task |
| DELETE | `/tasks/all` | Delete all tasks |
| GET | `/files` | List generated files |
| GET | `/files/download/{filename}` | Download a generated file |
| DELETE | `/files/{filename}` | Delete a generated file |
| DELETE | `/files/all/clear` | Delete all generated files |
| GET | `/preferences` | List all preferences |
| POST | `/preferences` | Save a preference |
| DELETE | `/preferences/all` | Clear all preferences |
| DELETE | `/preferences/{key}` | Delete one preference |
| GET | `/devices` | List connected devices |
| WS | `/ws/device/{id}` | Device Agent connection |
| WS | `/ws/ui/{id}` | Web UI live updates |

### Example: Send a command

```bash
curl -X POST https://your-app.railway.app/command \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: your_secret" \
  -d '{"command": "Search YouTube for lofi music playlists"}'
```

### Example: Save a preference via API

```bash
curl -X POST https://your-app.railway.app/preferences \
  -H "Content-Type: application/json" \
  -d '{"label": "Preferred language", "value": "Python"}'
```

---

## Browser Agent — Supported Actions

| Action | Example Command |
|---|---|
| Google search | `Search for the latest AI news` |
| Scrape a page | `Go to wikipedia.org/wiki/Philippines and summarize it` |
| YouTube search | `Search YouTube for Python tutorials` |
| Multi-step workflow | `Go to Hacker News and summarize the top 5 stories` |

---

## Device Agent — Supported Actions

| Action | Description | Example Parameters |
|---|---|---|
| `open_app` | Open an application | `{"app": "chrome"}` |
| `new_file` | Open blank doc in app | `{"app": "word"}` |
| `click` | Click at coordinates | `{"x": 500, "y": 300}` |
| `double_click` | Double-click | `{"x": 500, "y": 300}` |
| `right_click` | Right-click | `{"x": 500, "y": 300}` |
| `move` | Move mouse | `{"x": 500, "y": 300}` |
| `type` | Paste text | `{"text": "hello world"}` |
| `hotkey` | Press key combo | `{"keys": ["ctrl", "s"]}` |
| `scroll` | Scroll | `{"x": 960, "y": 540, "clicks": -3}` |
| `run_command` | Shell command | `{"command": "dir"}` |
| `wait` | Wait | `{"ms": 1000}` |
| `screenshot_and_return` | Capture screen | — |

### Vision Loop

1. Takes a screenshot
2. Sends to Cloud Brain for Qwen analysis
3. Receives next action
4. Repeats until goal complete or `max_steps` reached

Trigger with: `{"mode": "vision", "goal": "Open Chrome and search for AI news"}`

---

## Project Structure

```
aetherAI/
├── cloud_brain/
│   ├── main.py                        # Stage 5 — preference endpoints
│   ├── orchestrator.py                # Stage 5 — user context injection
│   ├── agent_router.py                # Stage 5 — memory_agent wired in
│   ├── memory.py                      # WAL mode, indexes, auto-cleanup
│   ├── config.py
│   ├── agents/
│   │   ├── __init__.py                # Stage 5 — memory kwarg added
│   │   ├── research_agent.py          # DDG JSON fallback, source URLs
│   │   ├── document_agent.py          # Picsum photos, 4-layout PPTX engine
│   │   ├── browser_agent.py           # Playwright + httpx fallback chain
│   │   ├── coding_agent.py            # Syntax validation, multi-file blocks
│   │   ├── automation_agent.py        # Param normalisation, sequence timeouts
│   │   └── memory_agent.py            # Stage 5 — NEW
│   └── utils/
│       ├── qwen_client.py             # Stage 5 — user_context in all calls
│       └── websocket_manager.py       # Per-session queues, dead session pruning
├── device_agent/
│   ├── agent.py                       # Action retry, JPEG screenshots, COM fix
│   ├── config.py
│   ├── aether_config.ini
│   ├── build_exe.bat
│   └── requirements.txt
├── web_ui/
│   └── index.html                     # Command history, settings modal, busy bar
├── database/
├── output/
├── requirements.txt
├── Procfile
├── .env.example
└── .gitignore
```

---

## Roadmap

- **Stage 1** ✅ Cloud Brain + API + Orchestrator + Research Agent
- **Stage 2** ✅ Document Agent (PPTX with photos, DOCX, XLSX — 6 themes)
- **Stage 3** ✅ Device Agent (vision loop, mouse/keyboard, app automation)
- **Stage 4** ✅ Browser Agent (Playwright — Google, YouTube, scraping, workflows) + Standalone EXE
- **Stage 5** ✅ Memory system (preferences, memory_agent, user context injection)
- **Stage 6** — ESP32 voice interface
- **Stage 7** — Web Dashboard v2 (multi-device management, per-device commands)

---

## Safety Notes

- Set `AETHER_API_KEY` in production — this prevents unauthorized access
- The same API key goes in `aether_config.ini` for the standalone exe
- Device Agent has pyautogui FAILSAFE enabled (move mouse to top-left corner to abort)
- Destructive actions (file deletion, system commands) will require confirmation in a future stage
- Each connected device shows up by `device_id` in the dashboard — give each person a unique ID
- Preferences are stored locally in SQLite — they never leave your Railway instance
