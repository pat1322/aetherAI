"""
AetherAI -- Qwen API Client
Uses Qwen (Alibaba DashScope) via the OpenAI-compatible endpoint.

ROUTING LOGIC (priority order):
  1. _is_open_chrome_command()  → automation_agent  (open real Chrome on PC)
       Catches: "open chrome", "launch chrome", "go to youtube", "go to reddit",
                "play on youtube", "open youtube", etc.
  2. _is_browser_task()         → browser_agent     (headless cloud scraping/search)
       Catches: "search youtube for X", "on youtube", Wikipedia URLs, etc.
  3. _is_open_app_command()     → automation_agent  (open Word/Excel/etc.)
  4. Qwen planner               → decides remaining tasks

KEY FIX v2:
  - "go to [site]" and "play on youtube" now correctly open real Chrome on the PC
  - "open chrome and google X" now generates a Google search URL in step 3
  - Separated navigation intent from research intent for YouTube/sites
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

    # Headless cloud browser tasks — research/scraping, no real PC browser needed
    BROWSER_KEYWORDS = [
        # YouTube research (NOT navigation — "go to youtube" is handled by OPEN_CHROME)
        "search youtube for", "youtube search for", "find on youtube",
        "youtube video about", "youtube channel",
        # Wikipedia
        "wikipedia", "wiki/",
        # Explicit scraping/research
        "scrape", "extract from", "read the article at",
        "summarize the page", "summarize the website",
        "summarize the article at",
        # Explicit web search (cloud, not PC browser)
        "search google for", "google search for",
        "search the web for", "search bing for",
    ]

    # Navigation intents — user wants real Chrome open on their PC
    OPEN_CHROME_KEYWORDS = [
        # Explicit browser open
        "open chrome", "launch chrome", "start chrome",
        "open browser", "launch browser",
        "open firefox", "launch firefox",
        "open edge", "launch edge",
        # "go to [site]" — navigation, not research
        "go to youtube", "open youtube",
        "go to reddit", "go to twitter", "go to x.com",
        "go to facebook", "go to instagram",
        "go to github", "go to google",
        "go to netflix", "go to spotify",
        "go to hacker news", "go to hackernews",
        "go to stackoverflow", "go to stack overflow",
        # Play/watch intents always need real browser
        "play on youtube", "watch on youtube", "watch youtube",
        "play youtube", "open youtube and",
        # Generic navigate/visit
        "navigate to", "visit the website",
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
        # Pure YouTube/Wikipedia/URL/search — no "open chrome" prefix
        return any(k in cmd_lc for k in self.BROWSER_KEYWORDS)

    def _is_open_chrome_command(self, cmd_lc: str) -> bool:
        """User wants to open Chrome/browser on their actual PC."""
        return any(k in cmd_lc for k in self.OPEN_CHROME_KEYWORDS)

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
        if self._is_open_chrome_command(cmd_lc):
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
        is_open_chrome = self._is_open_chrome_command(cmd_lc)
        is_browser     = self._is_browser_task(cmd_lc)
        is_coding      = any(k in cmd_lc for k in self.CODE_KEYWORDS)

        # ── Hard-route: "open chrome and go to X" → automation_agent ─────────
        # This opens the real Chrome on the PC, then navigates
        if is_open_chrome:
            return self._build_open_chrome_plan(command, cmd_lc)

        # ── Hard-route browser tasks (no open chrome prefix) ─────────────────
        if is_browser:
            return self._build_browser_plan(command, cmd_lc)

        # ── Hard override for open [Office app] commands ─────────────────────
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

            "browser_agent — headless Chromium (Playwright) running in the cloud\n"
            '  Search:   {"action":"search","query":"...","engine":"google|bing|duckduckgo"}\n'
            '  YouTube:  {"action":"youtube","query":"..."}\n'
            '  Scrape:   {"action":"scrape","url":"https://..."}\n'
            '  Workflow: {"action":"workflow","goal":"...","url":"https://..."}\n'
            "  Use for: YouTube, Wikipedia, any URL scraping, web search.\n"
            "  Do NOT use for: 'open chrome' — that's automation_agent.\n\n"

            "document_agent — create DOWNLOADABLE .pptx/.docx/.xlsx files\n"
            '  {"type": "presentation"|"document"|"spreadsheet", "topic": "..."}\n'
            "  Use ONLY when user wants a file to download. NEVER if user said 'open [app]'.\n\n"

            "coding_agent — write and save code\n"
            '  {"task": "...", "language": "python|c|c++|javascript|..."}\n\n'

            "automation_agent — control the real PC (mouse, keyboard, apps)\n"
            '  open_app: {"action":"open_app","parameters":{"app":"chrome|notepad|word|excel|powerpoint"}}\n'
            '  new_file: {"action":"new_file","parameters":{"app":"notepad|word|excel|powerpoint"}}\n'
            '  type:     {"action":"type","parameters":{"text":"__GENERATED_CONTENT__"}}\n'
            '  hotkey:   {"action":"hotkey","parameters":{"keys":["ctrl","s"]}}\n'
            '  wait:     {"action":"wait","parameters":{"ms":2000}}\n'
            '  run_cmd:  {"action":"run_command","parameters":{"command":"start https://youtube.com"}}\n\n'

            "ROUTING RULES:\n"
            "1. 'open chrome/browser/firefox' → automation_agent (open_app + run_command to navigate)\n"
            "2. YouTube/Wikipedia/URLs (no open chrome) → browser_agent\n"
            "3. 'open word/excel/ppt and write X' → automation_agent ONLY\n"
            "4. 'create/make a presentation/doc/spreadsheet' → document_agent\n"
            "5. 'write a python/C program' (no app) → coding_agent\n"
            "6. General research/news → research_agent\n"
            "7. NEVER write long content inline in JSON — use __GENERATED_CONTENT__\n"
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

    def _build_open_chrome_plan(self, command: str, cmd_lc: str) -> list[dict]:
        """
        Builds an automation_agent plan to open real Chrome on the PC and navigate.

        Handles:
          - "open chrome and google X"      → google.com/search?q=X
          - "open chrome and go to youtube" → youtube.com
          - "go to youtube and play X"      → youtube.com/results?search_query=X
          - "go to reddit"                  → reddit.com
          - any explicit https:// URL
        """
        dest = None

        # 1. Explicit URL in command
        url_match = re.search(r"https?://[^\s]+", command)
        if url_match:
            dest = url_match.group(0)

        # 2. "google X" / "and google X" → Google search URL
        if not dest:
            google_match = re.search(
                r"(?:^|and\s+|then\s+)google\s+(.+)$", cmd_lc
            )
            if google_match:
                query = google_match.group(1).strip()
                from urllib.parse import quote_plus
                dest = f"https://www.google.com/search?q={quote_plus(query)}"

        # 3. YouTube play/watch intent → YouTube search URL
        if not dest:
            yt_play_match = re.search(
                r"(?:play|watch|search|find)\s+(.+?)(?:\s+on youtube|\s+on yt|$)",
                cmd_lc
            )
            if yt_play_match and "youtube" in cmd_lc:
                query = yt_play_match.group(1).strip()
                from urllib.parse import quote_plus
                dest = f"https://www.youtube.com/results?search_query={quote_plus(query)}"

        # 4. "go to [site]" / "open [site]" / "visit [site]" pattern
        if not dest:
            go_match = re.search(
                r"(?:go to|open|visit|navigate to|and (?:go to|open|visit))\s+"
                r"((?:www\.)?[\w\-]+(?:\.[\w]{2,}[^\s]*)?)",
                cmd_lc
            )
            if go_match:
                site = go_match.group(1).strip()
                # Known bare names → .com
                KNOWN_SITES = {
                    "youtube": "youtube.com", "reddit": "reddit.com",
                    "twitter": "twitter.com", "instagram": "instagram.com",
                    "facebook": "facebook.com", "github": "github.com",
                    "google": "google.com", "netflix": "netflix.com",
                    "spotify": "spotify.com", "stackoverflow": "stackoverflow.com",
                    "hackernews": "news.ycombinator.com",
                    "hacker news": "news.ycombinator.com",
                }
                site_resolved = KNOWN_SITES.get(site, site)
                if "." not in site_resolved:
                    site_resolved = f"{site_resolved}.com"
                if not site_resolved.startswith("http"):
                    site_resolved = f"https://{site_resolved}"
                dest = site_resolved

        # Build base plan: open Chrome + wait
        plan = [
            {
                "step": 1,
                "agent": "automation_agent",
                "description": "Open Chrome browser",
                "parameters": {"action": "open_app", "parameters": {"app": "chrome"}}
            },
            {
                "step": 2,
                "agent": "automation_agent",
                "description": "Wait for Chrome to load",
                "parameters": {"action": "wait", "parameters": {"ms": 2500}}
            },
        ]

        if dest:
            plan.append({
                "step": 3,
                "agent": "automation_agent",
                "description": f"Navigate to {dest}",
                "parameters": {
                    "action": "run_command",
                    "parameters": {"command": f'start "" "{dest}"'}
                }
            })

        return plan

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

        # Multi-step workflow (go to site, navigate, etc.)
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
