# ⬡ AetherAI — Personal AI Agent System

> A cloud-hosted AI agent that acts as your digital employee.
> Controls computers, automates tasks, researches topics, and generates documents.

---

## Stage 1 — What's Running

| Component | Status | Description |
|---|---|---|
| Cloud Brain (FastAPI) | ✅ | API Gateway, WebSocket hub |
| Orchestrator | ✅ | Plans tasks with Qwen, routes to agents |
| Research Agent | ✅ | Web search + Qwen summarization |
| Document Agent | ✅ stub | Content generation via Qwen |
| Device Agent | ✅ | PC control via WebSocket |
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
git commit -m "Stage 1: Cloud Brain"
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
├── requirements.txt
├── Procfile                 # Railway start command
├── .env.example
└── .gitignore
```

---

## Roadmap

- **Stage 1** ✅ Cloud Brain + API + Orchestrator + Research Agent
- **Stage 2** — Full Document Agent (PPTX, DOCX, XLSX generation)
- **Stage 3** — Full Device Agent (vision loop, app control)
- **Stage 4** — Browser Agent (Playwright, Gmail, YouTube)
- **Stage 5** — Memory system (preferences, file registry)
- **Stage 6** — ESP32 voice interface
- **Stage 7** — Web Dashboard v2

---

## Safety Notes

- Set `AETHER_API_KEY` in production — this prevents unauthorized access
- Device Agent has pyautogui FAILSAFE enabled (move mouse to top-left corner to abort)
- Destructive actions (file deletion, system commands) will require confirmation in Stage 3
