"""
AetherAI — Qwen API Client  (Stage 5 — fully patched)

All fixes applied
─────────────────
FIX A  Added chat_with_image() — sends a screenshot as a base64-encoded
       image content block alongside the text prompt using the OpenAI-
       compatible multimodal message format.
       Requires a vision-capable Qwen model (qwen-vl-plus or qwen-vl-max).
       Set QWEN_VISION_MODEL in Railway env vars; falls back to QWEN_MODEL
       if not set (text-only fallback with a warning).

Original Stage 5 fixes retained:
  FIX 2  Strip trailing research_agent steps after terminal agents
  FIX 4  Screenshot commands hard-routed to automation_agent
  FIX 7  Hacker News keyword check in _build_browser_plan
"""

import json
import re

from openai import AsyncOpenAI
from config import settings

import logging
logger = logging.getLogger(__name__)


class QwenClient:

    def __init__(self):
        if not settings.QWEN_API_KEY:
            raise ValueError("QWEN_API_KEY is not set.")
        self._client = AsyncOpenAI(
            api_key=settings.QWEN_API_KEY,
            base_url=settings.QWEN_BASE_URL,
        )
        self.model        = settings.QWEN_MODEL
        self.vision_model = settings.QWEN_VISION_MODEL  # may equal QWEN_MODEL

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

    async def chat_with_image(
        self,
        system_prompt: str,
        user_message: str,
        image_base64: str,
        temperature: float = 0.1,
    ) -> str:
        """
        FIX A: Send a text prompt + base64 screenshot to a vision-capable
        Qwen model using the OpenAI-compatible multimodal content format.

        If the vision model is not set or is the same as the text model
        (i.e., a non-vision model), falls back gracefully to text-only chat
        with a warning.
        """
        # Build the multimodal user message
        user_content = [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{image_base64}",
                },
            },
            {
                "type": "text",
                "text": user_message,
            },
        ]

        try:
            response = await self._client.chat.completions.create(
                model=self.vision_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_content},
                ],
                temperature=temperature,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            # Some models don't support vision — fall back to text-only
            logger.warning(
                f"[QwenClient] chat_with_image failed ({e}). "
                "Falling back to text-only. Set QWEN_VISION_MODEL=qwen-vl-plus in env."
            )
            return await self.chat(system_prompt, user_message, temperature)

    # ── Keyword sets ──────────────────────────────────────────────────────────

    BROWSER_KEYWORDS = [
        "using browser agent", "use browser agent", "browser agent",
        "search youtube", "on youtube", "youtube for", "find on youtube",
        "youtube search", "youtube video", "youtube channel",
        "wikipedia", "wiki/",
        "go to http", "go to www", "go to reddit", "go to twitter",
        "go to facebook", "go to instagram", "go to hacker news",
        "go to github", "go to stackoverflow", "visit http",
        "navigate to", "open the website", "open the page",
        "search google for", "google for", "google search",
        "search the web for", "search bing",
        "scrape", "extract from", "read the article at",
        "summarize the page", "summarize the website",
        "summarize the article at",
    ]

    CHROME_WITH_ACTION_KEYWORDS = [
        "open chrome and", "launch chrome and", "open browser and",
        "open firefox and", "open chrome to", "open chrome then",
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

    MEMORY_KEYWORDS = [
        "remember that", "remember my", "save that", "store that",
        "note that", "my preference is", "i prefer", "i use", "i like",
        "what do you know about me", "what have you remembered",
        "show my preferences", "list my preferences",
        "forget my", "forget that", "delete my", "remove my",
        "clear my preferences", "clear all preferences",
        "recall my", "do you remember", "do you know my",
        "what is my", "what's my",
    ]

    SCREENSHOT_KEYWORDS = [
        "take a screenshot", "take screenshot", "screenshot of my screen",
        "capture screen", "capture my screen", "screenshot now",
        "take a screen capture", "screen capture",
        "take a screenshoot",
    ]

    REALTIME_KEYWORDS = [
        "what time is it", "what's the time", "current time",
        "what time in ", "time in ", "time now in", "time right now",
        "what day is it", "what's today", "today's date", "current date",
        "what date is it", "what year is it",
        "weather in ", "current weather", "what's the weather",
        "temperature in ", "forecast for",
        "what is the time", "tell me the time",
    ]

    def _is_browser_task(self, cmd_lc: str) -> bool:
        if any(k in cmd_lc for k in self.BROWSER_KEYWORDS):
            return True
        if any(k in cmd_lc for k in self.CHROME_WITH_ACTION_KEYWORDS):
            return True
        return False

    def _is_memory_task(self, cmd_lc: str) -> bool:
        return any(k in cmd_lc for k in self.MEMORY_KEYWORDS)

    def _is_open_app_command(self, cmd_lc: str) -> bool:
        OPEN_VERBS  = ["open ", "launch ", "start "]
        OFFICE_APPS = [
            "word", "excel", "powerpoint", "ppt", "notepad", "notepad++",
            "chrome", "firefox", "edge",
        ]
        starts_with_open = any(cmd_lc.startswith(v) or f" {v}" in cmd_lc for v in OPEN_VERBS)
        mentions_app     = any(app in cmd_lc for app in OFFICE_APPS)
        has_browser_action = any(k in cmd_lc for k in self.CHROME_WITH_ACTION_KEYWORDS)
        return starts_with_open and mentions_app and not has_browser_action

    def _is_screenshot_task(self, cmd_lc: str) -> bool:
        return any(k in cmd_lc for k in self.SCREENSHOT_KEYWORDS)

    def _is_realtime_task(self, cmd_lc: str) -> bool:
        return any(k in cmd_lc for k in self.REALTIME_KEYWORDS)

    # ── Command classifier ────────────────────────────────────────────────────

    async def classify_command(self, command: str, user_context: str = "") -> str:
        cmd_lc = command.lower()

        if self._is_memory_task(cmd_lc):
            return "task"
        if any(k in cmd_lc for k in self.CODE_KEYWORDS):
            return "task"
        if self._is_browser_task(cmd_lc):
            return "task"
        if self._is_screenshot_task(cmd_lc):
            return "task"
        if self._is_realtime_task(cmd_lc):
            return "task"

        ctx_block = f"\n\nUser context:\n{user_context}" if user_context else ""
        system = (
            "You are a command classifier for AetherAI. "
            "Classify the user input as either 'chat' or 'task'.\n\n"
            "'chat' = simple questions, greetings, factual queries, math, definitions.\n"
            "'task' = anything requiring creating files, research, computer control, "
            "writing code, browsing the web, saving/recalling preferences, or multi-step work.\n\n"
            "Return ONLY the single word: chat OR task. Nothing else."
        )
        result = await self.chat(system, command + ctx_block, temperature=0.0)
        return "chat" if "chat" in result.strip().lower() else "task"

    # ── Task planner ──────────────────────────────────────────────────────────

    async def plan_task(self, command: str, user_context: str = "") -> list[dict]:
        cmd_lc      = command.lower()
        is_open_app = self._is_open_app_command(cmd_lc)
        is_browser  = self._is_browser_task(cmd_lc)
        is_coding   = any(k in cmd_lc for k in self.CODE_KEYWORDS)
        is_memory   = self._is_memory_task(cmd_lc)

        if self._is_realtime_task(cmd_lc) and not is_browser:
            return [{"step": 1, "agent": "browser_agent",
                     "description": f"Search for: {command}",
                     "parameters": {"action": "search", "query": command, "engine": "google"}}]

        if is_memory:
            return [{
                "step": 1, "agent": "memory_agent",
                "description": f"Memory operation: {command}",
                "parameters": {"query": command},
            }]

        if self._is_screenshot_task(cmd_lc):
            return [{
                "step": 1, "agent": "automation_agent",
                "description": "Take a screenshot of the screen",
                "parameters": {"action": "screenshot_and_return"},
            }]

        if is_browser:
            return self._build_browser_plan(command, cmd_lc)

        no_doc_agent_rule = ""
        if is_open_app:
            no_doc_agent_rule = (
                "\n⚠️ CRITICAL OVERRIDE: This command says 'open [app]'. "
                "You MUST use automation_agent to control the PC. "
                "You MUST NOT use document_agent under any circumstances.\n"
            )

        ctx_block = f"\n\nUser preferences (use these when relevant):\n{user_context}" if user_context else ""

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
            "  Use ONLY when user wants a file to download. NEVER if user said 'open [app]'.\n"
            "  ⚠️ document_agent is a TERMINAL step — do NOT add research steps after it.\n\n"

            "coding_agent — write and save code\n"
            '  {"task": "...", "language": "python|c|c++|javascript|..."}\n'
            "  ⚠️ coding_agent is a TERMINAL step — do NOT add research steps after it.\n\n"

            "automation_agent — control the PC\n"
            '  open_app: {"action":"open_app","parameters":{"app":"chrome|edge|firefox|calculator|paint|explorer|notepad|word|excel|powerpoint|vlc|any-app-name"}}\n'
            '  new_file: {"action":"new_file","parameters":{"app":"notepad|word|excel|powerpoint|notepad++"}}\n'
            '  type:     {"action":"type","parameters":{"text":"__GENERATED_CONTENT__"}}\n'
            '  hotkey:   {"action":"hotkey","parameters":{"keys":["ctrl","s"]}}\n'
            '  wait:     {"action":"wait","parameters":{"ms":2000}}\n'
            '  screenshot: {"action":"screenshot_and_return"}\n\n'

            "memory_agent — save/recall/forget user preferences and personal facts\n"
            '  {"query": "full user statement"}\n'
            "  Use for: 'remember that', 'what do you know about me', 'forget my X', 'my preference is'.\n\n"

            "ROUTING RULES:\n"
            "1. YouTube/Wikipedia/URLs → browser_agent. 'open chrome and search X' → browser_agent. 'open chrome' ALONE → automation_agent (open_app)\n"
            "2. 'open word/excel/ppt and write X' → automation_agent ONLY\n"
            "3. 'create/make a presentation/doc/spreadsheet' → document_agent (single step, no follow-up)\n"
            "4. 'write a python/C program' (no app) → coding_agent (single step, no follow-up)\n"
            "5. General research/news → research_agent\n"
            "6. NEVER write long content inline in JSON — use __GENERATED_CONTENT__\n"
            "7. 'remember/recall/forget/what do you know about me' → memory_agent\n"
            "8. 'take a screenshot / capture screen' → automation_agent with screenshot_and_return\n"
        )

        raw = await self.chat(
            system_prompt,
            f'Plan this task: "{command}"{ctx_block}',
            temperature=0.2,
        )
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

        try:
            plan = json.loads(raw)
            if not isinstance(plan, list):
                raise ValueError("Not a list")

            if is_open_app:
                plan = [s for s in plan if s.get("agent") != "document_agent"]
                for idx, step in enumerate(plan, 1):
                    step["step"] = idx

            plan = self._strip_trailing_steps(plan)
            return plan
        except (json.JSONDecodeError, ValueError):
            return [{
                "step": 1, "agent": "research_agent",
                "description": f"Process: {command}",
                "parameters": {"query": command},
            }]

    def _strip_trailing_steps(self, plan: list) -> list:
        TERMINAL_AGENTS = {"document_agent", "coding_agent"}
        if not plan:
            return plan
        last_terminal_idx = -1
        for i, step in enumerate(plan):
            if step.get("agent") in TERMINAL_AGENTS:
                last_terminal_idx = i
        if last_terminal_idx < 0:
            return plan
        trailing = plan[last_terminal_idx + 1:]
        if not trailing:
            return plan
        if all(s.get("agent") == "research_agent" for s in trailing):
            return plan[:last_terminal_idx + 1]
        return plan

    def _build_browser_plan(self, command: str, cmd_lc: str) -> list[dict]:
        SITE_MAP = {
            "youtube": "https://youtube.com", "facebook": "https://facebook.com",
            "twitter": "https://twitter.com", "instagram": "https://instagram.com",
            "reddit": "https://reddit.com",   "github": "https://github.com",
            "stackoverflow": "https://stackoverflow.com",
            "netflix": "https://netflix.com", "amazon": "https://amazon.com",
            "gmail": "https://gmail.com",     "linkedin": "https://linkedin.com",
            "tiktok": "https://tiktok.com",   "spotify": "https://spotify.com",
        }

        if "youtube" in cmd_lc:
            YOUTUBE_NAV    = ["go to youtube", "open youtube", "navigate to youtube",
                              "visit youtube", "and go to youtube", "go on youtube"]
            YOUTUBE_SEARCH = ["search youtube for", "youtube for", "on youtube",
                               "find on youtube", "youtube search", "search on youtube"]
            has_nav    = any(k in cmd_lc for k in YOUTUBE_NAV)
            has_search = any(k in cmd_lc for k in YOUTUBE_SEARCH)

            if has_nav and not has_search:
                return [{"step": 1, "agent": "browser_agent",
                         "description": "Navigate to YouTube",
                         "parameters": {"action": "workflow", "goal": "open youtube",
                                        "url": "https://youtube.com"}}]

            query = cmd_lc
            for pat in YOUTUBE_SEARCH + ["open chrome and", "launch chrome and",
                                          "open browser and", "search youtube"]:
                query = re.sub(rf".*{re.escape(pat)}\s*", "", query,
                               flags=re.IGNORECASE).strip()
            if not query or len(query) < 2:
                return [{"step": 1, "agent": "browser_agent",
                         "description": "Navigate to YouTube",
                         "parameters": {"action": "workflow", "goal": "open youtube",
                                        "url": "https://youtube.com"}}]
            return [{"step": 1, "agent": "browser_agent",
                     "description": f"Search YouTube for: {query}",
                     "parameters": {"action": "youtube", "query": query}}]

        if "wikipedia" in cmd_lc or "wiki/" in cmd_lc:
            url_match = re.search(r"(https?://[^\s]+|wikipedia\.org/wiki/[^\s]+)", cmd_lc)
            if url_match:
                url = url_match.group(1)
                if not url.startswith("http"): url = "https://" + url
                return [{"step": 1, "agent": "browser_agent",
                         "description": f"Read Wikipedia: {url}",
                         "parameters": {"action": "scrape", "url": url}}]
            return [{"step": 1, "agent": "browser_agent",
                     "description": f"Search Wikipedia: {command}",
                     "parameters": {"action": "search", "query": command, "engine": "google"}}]

        if any(k in cmd_lc for k in ("hacker news", "hackernews", "ycombinator", "news.ycombinator")):
            return [{"step": 1, "agent": "browser_agent",
                     "description": "Fetch top Hacker News stories",
                     "parameters": {"action": "workflow", "goal": command}}]

        url_match = re.search(r"https?://[^\s]+", command)
        if url_match:
            url = url_match.group(0)
            return [{"step": 1, "agent": "browser_agent",
                     "description": f"Read page: {url}",
                     "parameters": {"action": "scrape", "url": url}}]

        if any(k in cmd_lc for k in ["search google", "google for", "google search",
                                      "search the web", "search bing"]):
            query = re.sub(
                r".*(search google for|google for|search the web for|search bing for|google search)\s*",
                "", cmd_lc, flags=re.IGNORECASE
            ).strip() or command
            engine = "bing" if "bing" in cmd_lc else "google"
            return [{"step": 1, "agent": "browser_agent",
                     "description": f"Search {engine}: {query}",
                     "parameters": {"action": "search", "query": query, "engine": engine}}]

        dest_match = re.search(
            r"(?:go to|open|visit|navigate to)\s+([\w\-\.]+\.[\w]{2,}[^\s]*)", cmd_lc
        )
        if dest_match:
            url = dest_match.group(1)
            if not url.startswith("http"): url = "https://" + url
            return [{"step": 1, "agent": "browser_agent",
                     "description": f"Browser workflow: {command}",
                     "parameters": {"action": "workflow", "goal": command, "url": url}}]

        nav_plain = re.search(r"(?:go to|open|visit|navigate to|launch)\s+(\w+)", cmd_lc)
        if nav_plain:
            site_key = nav_plain.group(1).lower()
            if site_key in SITE_MAP:
                return [{"step": 1, "agent": "browser_agent",
                         "description": f"Navigate to {site_key.capitalize()}",
                         "parameters": {"action": "workflow", "goal": f"open {site_key}",
                                        "url": SITE_MAP[site_key]}}]

        return [{"step": 1, "agent": "browser_agent",
                 "description": f"Browser search: {command}",
                 "parameters": {"action": "search", "query": command, "engine": "google"}}]

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
        user   = f"{context}\n\nContent:\n{content}" if context else content
        return await self.chat(system, user, temperature=0.5)

    async def answer(self, question: str, context: str = "", user_context: str = "") -> str:
        system = (
            "You are AetherAI, a personal AI agent assistant built by Patrick Perez, "
            "a 26-year-old software engineer from the Philippines. "
            "Answer clearly and directly."
        )
        parts = []
        if user_context: parts.append(f"User preferences and facts:\n{user_context}")
        if context:      parts.append(f"Context:\n{context}")
        parts.append(f"Question: {question}")
        user = "\n\n".join(parts)
        return await self.chat(system, user)
