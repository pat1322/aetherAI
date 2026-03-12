# ⬡ AetherAI — Personal AI Agent System

> A cloud-hosted AI agent that acts as your digital employee.
> Controls computers, automates tasks, researches topics, and generates documents.

---

## Stage 3 — What's Running

| Component | Status | Description |
|---|---|---|
| Cloud Brain (FastAPI) | ✅ | API Gateway, WebSocket hub |
| Orchestrator | ✅ | Plans tasks with Qwen, routes to agents |
| Research Agent | ✅ | Web search + Qwen summarization |
| Document Agent | ✅ | PowerPoint, Word, and Excel generation via Qwen |
| Device Agent | ✅ | PC control via WebSocket (mouse, keyboard, vision loop) |
| Automation Agent | ✅ | Single actions, sequences, and vision loop execution |
| Web UI | ✅ | Command center dashboard |
| Memory (SQLite) | ✅ | Task + step persistence |

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
cd cloud_brain
uvicorn main:app --reload --port 8000
```

Open: http://localhost:8000/ui/index.html

### 3. Run the Device Agent on your PC

```bash
cd device_agent
pip install -r requirements.txt

# Set the cloud URL (for local dev, default works)
python agent.py
```

---

## Deploy to Railway

### Step 1: Push to GitHub

```bash
git add .
git commit -m "Stage 3: Device Agent + Vision Loop"
git push origin main
```

### Step 2: Create Railway project

1. Go to [railway.app](https://railway.app)
2. New Project → Deploy from GitHub repo → select `aetherAI`
3. Railway auto-detects the `Procfile`

### Step 3: Set environment variables in Railway

In your Railway service → Variables tab:

```
QWEN_API_KEY    = your_key_here
AETHER_API_KEY  = your_secret_here
QWEN_MODEL      = qwen-plus
```

### Step 4: Deploy

Railway deploys automatically on every push to `main`.

### Step 5: Connect Device Agent to Railway

Edit `device_agent/config.py` or set env var:
```bash
export AETHER_CLOUD_URL=https://your-app.railway.app
export AETHER_API_KEY=your_secret_here
python agent.py
```

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
  -d '{"command": "Research the latest AI breakthroughs"}'
```

---

## Device Agent — Supported Actions

The Device Agent supports the following actions via WebSocket:

| Action | Description | Example Parameters |
|---|---|---|
| `open_app` | Open an application | `{"app": "notepad"}` |
| `click` | Click at coordinates | `{"x": 500, "y": 300}` |
| `double_click` | Double-click at coordinates | `{"x": 500, "y": 300}` |
| `right_click` | Right-click at coordinates | `{"x": 500, "y": 300}` |
| `move` | Move mouse to coordinates | `{"x": 500, "y": 300}` |
| `type` | Type text | `{"text": "hello world"}` |
| `type_special` | Paste non-ASCII text | `{"text": "special chars"}` |
| `hotkey` | Press key combination | `{"keys": ["ctrl", "s"]}` |
| `scroll` | Scroll at position | `{"x": 960, "y": 540, "clicks": -3}` |
| `run_command` | Run a shell command | `{"command": "dir"}` |
| `wait` | Wait for a duration | `{"ms": 1000}` |
| `screenshot_and_return` | Capture and return screenshot | — |

### Vision Loop

The Device Agent supports a vision loop for complex, multi-step goals:

1. Takes a screenshot
2. Sends it to Cloud Brain for Qwen analysis
3. Receives the next action to execute
4. Repeats until the goal is complete or `max_steps` is reached

Trigger with: `{"mode": "vision", "goal": "Open Chrome and search for AI news"}`

---

## Project Structure

```
aetherAI/
├── cloud_brain/
│   ├── main.py              # FastAPI app
│   ├── orchestrator.py      # Task planner
│   ├── agent_router.py      # Routes to agents
│   ├── memory.py            # SQLite storage
│   ├── config.py            # Settings
│   ├── agents/
│   │   ├── __init__.py      # BaseAgent
│   │   ├── research_agent.py
│   │   ├── document_agent.py
│   │   ├── browser_agent.py
│   │   ├── coding_agent.py
│   │   └── automation_agent.py
│   └── utils/
│       ├── qwen_client.py
│       └── websocket_manager.py
├── device_agent/
│   ├── agent.py             # Runs on your PC
│   ├── config.py
│   └── requirements.txt
├── web_ui/
│   └── index.html           # Dashboard
├── database/                # Auto-created
├── output/                  # Generated files (auto-created)
├── requirements.txt
├── Procfile                 # Railway start command
├── .env.example
└── .gitignore
```

---

## Roadmap

- **Stage 1** ✅ Cloud Brain + API + Orchestrator + Research Agent
- **Stage 2** ✅ Full Document Agent (PPTX, DOCX, XLSX generation)
- **Stage 3** ✅ Full Device Agent (vision loop, mouse/keyboard control, app automation)
- **Stage 4** — Browser Agent (Playwright, Gmail, YouTube)
- **Stage 5** — Memory system (preferences, file registry)
- **Stage 6** — ESP32 voice interface
- **Stage 7** — Web Dashboard v2

---

## Safety Notes

- Set `AETHER_API_KEY` in production — this prevents unauthorized access
- Device Agent has pyautogui FAILSAFE enabled (move mouse to top-left corner to abort)
- Destructive actions (file deletion, system commands) will require confirmation in a future stage
