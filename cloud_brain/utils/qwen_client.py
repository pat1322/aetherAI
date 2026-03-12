"""
AetherAI -- Qwen API Client
Uses Qwen (Alibaba DashScope) via the OpenAI-compatible endpoint.

BROWSER AGENT FIX:
  Added hard pre-routing for browser tasks — same pattern as coding tasks.
  If the command contains browser keywords (youtube, google, go to, visit, open chrome,
  search youtube, wikipedia, etc.) the planner is forced to use browser_agent.
  This prevents Qwen from falling back to research_agent for browser tasks.
"""

import json
import re

from openai import AsyncOpenAI
from config import settings


class QwenClient:

    def __init__(self):
        if not settings.QWEN_API_KEY:
            raise ValueError("QWEN_API_KEY is not set.")
        self._client = AsyncOpenAI(
            api_key=settings.QWEN_API_KEY,
            base_url=settings.QWEN_BASE_URL,
        )
        self.model = settings.QWEN_MODEL

    async def chat(self, system_prompt: str, user_message: str, temperature: float = 0.7) -> str:
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_message},
            ],
            temperature=temperature,
        )
        return response.choices[0].message.content.strip()

    # ── Keyword sets ──────────────────────────────────────────────────────────

    BROWSER_KEYWORDS = [
        # explicit browser agent mentions
        "using browser agent", "use browser agent", "browser agent",
        # YouTube
        "search youtube", "on youtube", "youtube for", "find on youtube",
        "youtube search", "youtube video", "youtube channel",
        # Wikipedia
        "wikipedia", "wiki/",
        # Chrome / browser actions
        "open chrome", "open browser", "open firefox",
        "open chrome and", "launch chrome",
        # URL navigation
        "go to http", "go to www", "go to reddit", "go to twitter",
        "go to facebook", "go to instagram", "go to hacker news",
        "go to github", "go to stackoverflow", "visit http",
        "navigate to", "open the website", "open the page",
        # Google search (explicit)
        "search google for", "google for", "google search",
        "search the web for", "search bing",
        # Scraping
        "scrape", "extract from", "read the article at",
        "summarize the page", "summarize the website",
        "summarize the article at",
    ]

    CODE_KEYWORDS = [
        "write a program", "create a program", "make a program",
        "write a script", "create a script",
        "write code", "create code", "generate code",
        "code that", "program that", "script that",
        "write a python", "write a c ", "write a c++", "write a java",
        "write a function", "create a function",
        "write an algorithm", "implement a",
    ]

    def _is_browser_task(self, cmd_lc: str) -> bool:
        return any(k in cmd_lc for k in self.BROWSER_KEYWORDS)

    def _is_open_app_command(self, cmd_lc: str) -> bool:
        OPEN_VERBS = ["open ", "launch ", "start "]
        OFFICE_APPS = ["word", "excel", "powerpoint", "ppt", "notepad", "notepad++"]
        starts_with_open = any(cmd_lc.startswith(v) or f" {v}" in cmd_lc for v in OPEN_VERBS)
        mentions_app = any(app in cmd_lc for app in OFFICE_APPS)
        return starts_with_open and mentions_app

    # ── Command classifier ────────────────────────────────────────────────────

    async def classify_command(self, command: str) -> str:
        cmd_lc = command.lower()
        if any(k in cmd_lc for k in self.CODE_KEYWORDS):
            return "task"
        if self._is_browser_task(cmd_lc):
            return "task"
        system = (
            "You are a command classifier for AetherAI. "
            "Classify the user input as either 'chat' or 'task'.\n\n"
            "'chat' = simple questions, greetings, factual queries, math, definitions.\n"
            "'task' = anything requiring creating files, research, computer control, "
            "writing code, browsing the web, or multi-step work.\n\n"
            "Return ONLY the single word: chat OR task. Nothing else."
        )
        result = await self.chat(system, command, temperature=0.0)
        return "chat" if "chat" in result.strip().lower() else "task"

    # ── Task planner ──────────────────────────────────────────────────────────

    async def plan_task(self, command: str) -> list[dict]:
        cmd_lc = command.lower()
        is_open_app    = self._is_open_app_command(cmd_lc)
        is_browser     = self._is_browser_task(cmd_lc)
        is_coding      = any(k in cmd_lc for k in self.CODE_KEYWORDS)

        # ── Hard-route browser tasks directly — never let Qwen second-guess ──
        if is_browser:
            return self._build_browser_plan(command, cmd_lc)

        # ── Hard override for open [app] commands ────────────────────────────
        no_doc_agent_rule = ""
        if is_open_app:
            no_doc_agent_rule = (
                "\n⚠️ CRITICAL OVERRIDE: This command says 'open [app]'. "
                "You MUST use automation_agent to control the PC. "
                "You MUST NOT use document_agent under any circumstances.\n"
            )

        system_prompt = (
            "You are AetherAI's task planner built by Patrick Perez.\n"
            "Return ONLY a valid JSON array of steps. No explanation. No markdown fences.\n\n"
            + no_doc_agent_rule +
            "Each step: {step, agent, description, parameters}\n\n"

            "AGENTS:\n\n"

            "research_agent — DuckDuckGo web search + Qwen summarization\n"
            '  {"query": "..."}\n'
            "  Use for: general research, factual questions, news summaries.\n"
            "  Do NOT use for: YouTube, Wikipedia URLs, Chrome, browser navigation.\n\n"

            "browser_agent — real Chromium browser (Playwright)\n"
            '  Search:   {"action":"search","query":"...","engine":"google|bing|duckduckgo"}\n'
            '  YouTube:  {"action":"youtube","query":"..."}\n'
            '  Scrape:   {"action":"scrape","url":"https://..."}\n'
            '  Workflow: {"action":"workflow","goal":"...","url":"https://..."}\n'
            "  Use for: YouTube, Wikipedia, Chrome, any URL navigation, web scraping.\n\n"

            "document_agent — create DOWNLOADABLE .pptx/.docx/.xlsx files\n"
            '  {"type": "presentation"|"document"|"spreadsheet", "topic": "..."}\n'
            "  Use ONLY when user wants a file to download. NEVER if user said 'open [app]'.\n\n"

            "coding_agent — write and save code\n"
            '  {"task": "...", "language": "python|c|c++|javascript|..."}\n\n'

            "automation_agent — control the PC\n"
            '  new_file: {"action":"new_file","parameters":{"app":"notepad|word|excel|powerpoint|notepad++"}}\n'
            '  type:     {"action":"type","parameters":{"text":"__GENERATED_CONTENT__"}}\n'
            '  hotkey:   {"action":"hotkey","parameters":{"keys":["ctrl","s"]}}\n'
            '  wait:     {"action":"wait","parameters":{"ms":2000}}\n\n'

            "ROUTING RULES:\n"
            "1. YouTube/Wikipedia/Chrome/URLs → browser_agent\n"
            "2. 'open word/excel/ppt and write X' → automation_agent ONLY\n"
            "3. 'create/make a presentation/doc/spreadsheet' → document_agent\n"
            "4. 'write a python/C program' (no app) → coding_agent\n"
            "5. General research/news → research_agent\n"
            "6. NEVER write long content inline in JSON — use __GENERATED_CONTENT__\n"
        )

        raw = await self.chat(system_prompt, f'Plan this task: "{command}"', temperature=0.2)
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

        try:
            plan = json.loads(raw)
            if not isinstance(plan, list):
                raise ValueError("Not a list")

            # Safety net: strip document_agent if open_app command
            if is_open_app:
                plan = [s for s in plan if s.get("agent") != "document_agent"]
                for idx, step in enumerate(plan, 1):
                    step["step"] = idx

            return plan
        except (json.JSONDecodeError, ValueError):
            return [{
                "step": 1, "agent": "research_agent",
                "description": f"Process: {command}",
                "parameters": {"query": command},
            }]

    def _build_browser_plan(self, command: str, cmd_lc: str) -> list[dict]:
        """
        Build a browser_agent plan directly without calling Qwen,
        so it can never be misrouted to research_agent.
        """
        # YouTube
        if "youtube" in cmd_lc:
            query = re.sub(
                r".*(search youtube for|on youtube|youtube for|find.*youtube|youtube search)\s*",
                "", cmd_lc, flags=re.IGNORECASE
            ).strip() or command
            return [{
                "step": 1,
                "agent": "browser_agent",
                "description": f"Search YouTube for: {query}",
                "parameters": {"action": "youtube", "query": query},
            }]

        # Wikipedia
        if "wikipedia" in cmd_lc or "wiki/" in cmd_lc:
            # Extract URL if present
            url_match = re.search(r"(https?://[^\s]+|wikipedia\.org/wiki/[^\s]+)", cmd_lc)
            if url_match:
                url = url_match.group(1)
                if not url.startswith("http"):
                    url = "https://" + url
                return [{
                    "step": 1,
                    "agent": "browser_agent",
                    "description": f"Read Wikipedia: {url}",
                    "parameters": {"action": "scrape", "url": url},
                }]
            else:
                return [{
                    "step": 1,
                    "agent": "browser_agent",
                    "description": f"Search Wikipedia: {command}",
                    "parameters": {"action": "search", "query": command, "engine": "google"},
                }]

        # Explicit URL
        url_match = re.search(r"https?://[^\s]+", command)
        if url_match:
            url = url_match.group(0)
            return [{
                "step": 1,
                "agent": "browser_agent",
                "description": f"Read page: {url}",
                "parameters": {"action": "scrape", "url": url},
            }]

        # Google / web search
        if any(k in cmd_lc for k in ["search google", "google for", "google search",
                                      "search the web", "search bing"]):
            query = re.sub(
                r".*(search google for|google for|search the web for|search bing for|google search)\s*",
                "", cmd_lc, flags=re.IGNORECASE
            ).strip() or command
            engine = "bing" if "bing" in cmd_lc else "google"
            return [{
                "step": 1,
                "agent": "browser_agent",
                "description": f"Search {engine}: {query}",
                "parameters": {"action": "search", "query": query, "engine": engine},
            }]

        # Multi-step workflow (go to site, open chrome, navigate, etc.)
        # Extract destination if possible
        dest_match = re.search(
            r"(?:go to|open|visit|navigate to)\s+([\w\-\.]+\.[\w]{2,}[^\s]*)",
            cmd_lc
        )
        if dest_match:
            url = dest_match.group(1)
            if not url.startswith("http"):
                url = "https://" + url
            return [{
                "step": 1,
                "agent": "browser_agent",
                "description": f"Browser workflow: {command}",
                "parameters": {"action": "workflow", "goal": command, "url": url},
            }]

        # Generic browser search fallback
        return [{
            "step": 1,
            "agent": "browser_agent",
            "description": f"Browser search: {command}",
            "parameters": {"action": "search", "query": command, "engine": "google"},
        }]

    # ── Content generator ─────────────────────────────────────────────────────

    async def generate_content(self, command: str, content_type: str = "text") -> str:
        system = (
            "You are a creative writing assistant. Your ONLY job is to produce the requested written content. "
            "NEVER mention files, apps, computers, or what you can/cannot do. "
            "NEVER start with phrases like 'I can\\'t', 'Here is', 'Sure!', 'Certainly', or any preamble. "
            "NEVER refer to the fact that you are an AI. "
            "Output ONLY the raw content itself. Begin writing immediately."
        )
        clean_cmd = re.sub(
            r"\b(open|launch|start|create|use)\s+(notepad\+\+|notepad|word|excel|powerpoint|ppt|a file|a new file|an?\s+app)\s*(and|then|to)?\s*",
            "", command, flags=re.IGNORECASE
        ).strip() or command
        return await self.chat(system, f"Write this: {clean_cmd}", temperature=0.7)

    # ── Utilities ─────────────────────────────────────────────────────────────

    async def summarize(self, content: str, context: str = "") -> str:
        system = "You are a concise summarizer. Summarize clearly and briefly."
        user = f"{context}\n\nContent:\n{content}" if context else content
        return await self.chat(system, user, temperature=0.5)

    async def answer(self, question: str, context: str = "") -> str:
        system = (
            "You are AetherAI, a personal AI agent assistant built by Patrick Perez, "
            "a 26-year-old software engineer from the Philippines. "
            "Answer clearly and directly."
        )
        user = f"Context:\n{context}\n\nQuestion: {question}" if context else question
        return await self.chat(system, user)
