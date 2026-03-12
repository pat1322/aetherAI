# ⬡ AetherAI — Personal AI Agent System

> A cloud-hosted AI agent that acts as your digital employee.
> Controls computers, automates tasks, researches topics, browses the web, and generates documents.

---

## Stage 4 — What's Running

| Component | Status | Description |
|---|---|---|
| Cloud Brain (FastAPI) | ✅ | API Gateway, WebSocket hub |
| Orchestrator | ✅ | Plans tasks with Qwen, routes to agents |
| Research Agent | ✅ | DuckDuckGo web search + Qwen summarization |
| Document Agent | ✅ | PowerPoint (with Unsplash photos), Word, Excel — 6 random themes |
| Browser Agent | ✅ | Playwright — Google, YouTube, scraping, multi-step workflows |
| Device Agent | ✅ | PC control via WebSocket (mouse, keyboard, vision loop) |
| Automation Agent | ✅ | Single actions, sequences, and vision loop execution |
| Web UI | ✅ | Command center dashboard |
| Memory (SQLite) | ✅ | Task + step persistence |
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
git commit -m "Stage 4: Browser Agent + Standalone EXE"
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

### Step 4: Install Playwright on Railway

Add to your `Procfile` or a `railway.toml` post-deploy command:
```
playwright install chromium --with-deps
```

Or add to your startup script. Railway runs on Linux so chromium works fine.

### Step 5: Connect Device Agent to Railway

```bash
export AETHER_CLOUD_URL=https://your-app.railway.app
export AETHER_API_KEY=your_secret_here
python agent.py
```

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
device_id = johns-laptop         ; unique name per person
api_key   = your_shared_api_key  ; matches AETHER_API_KEY on Railway
```

Then they just double-click `AetherAI_Agent.exe`. Their device appears in the AetherAI dashboard and you can send commands to it.

### Optional: Auto-start on Windows boot

1. Press `Win + R` → type `shell:startup` → press Enter
2. Drop a shortcut to `AetherAI_Agent.exe` in that folder
3. It will connect automatically on every login

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| GET | `/` | System info |
| GET | `/health` | Health check |
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
│   ├── main.py
│   ├── orchestrator.py
│   ├── agent_router.py
│   ├── memory.py
│   ├── config.py
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── research_agent.py
│   │   ├── document_agent.py      # PPTX (with photos), DOCX, XLSX — 6 themes
│   │   ├── browser_agent.py       # Stage 4 — Playwright
│   │   ├── coding_agent.py
│   │   └── automation_agent.py
│   └── utils/
│       ├── qwen_client.py
│       └── websocket_manager.py
├── device_agent/
│   ├── agent.py                   # Stage 4 — COM fix, standalone exe support
│   ├── config.py                  # Dev mode config
│   ├── aether_config.ini          # Standalone exe config (distribute with exe)
│   ├── build_exe.bat              # Build AetherAI_Agent.exe
│   └── requirements.txt
├── web_ui/
│   └── index.html
├── database/
├── output/
├── requirements.txt               # Updated with playwright
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
- **Stage 5** — Memory system (preferences, file registry, user profiles)
- **Stage 6** — ESP32 voice interface
- **Stage 7** — Web Dashboard v2 (multi-device management, per-device commands)

---

## Safety Notes

- Set `AETHER_API_KEY` in production — this prevents unauthorized access
- The same API key goes in `aether_config.ini` for the standalone exe
- Device Agent has pyautogui FAILSAFE enabled (move mouse to top-left corner to abort)
- Destructive actions (file deletion, system commands) will require confirmation in a future stage
- Each connected device shows up by `device_id` in the dashboard — give each person a unique ID
