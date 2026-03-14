"""
AetherAI — Qwen API Client  (Stage 5 — patch 3)

Routing overhaul — agent identity fix
──────────────────────────────────────
FIX P5  Creative writing (haiku, poem, story, joke, song, essay, letter)
        was routing to task → research_agent + coding_agent, producing a
        Python script instead of actual creative content. These are now
        hard-routed to CHAT so Qwen answers directly.

FIX P6  Agent identities are now clearly separated:

        CHAT
          • Simple Q&A, factual questions, math, translations
          • Creative writing: poems, haikus, stories, jokes, songs, essays
          • Explanations and definitions
          • No web access — pure Qwen knowledge

        RESEARCH AGENT  (keyword: "research" / academic intent)
          • Triggered ONLY by explicit research intent:
            "research X", "find studies on X", "academic sources on X",
            "thesis about X", "literature review", "cite sources"
          • Multi-source DuckDuckGo search
          • Returns real URLs as citations
          • Identity: like a thesis researcher

        BROWSER AGENT  (real-time web + scraping)
          • Triggered by: specific URLs to scrape, YouTube searches,
            Hacker News, real-time prices/news/weather,
            "summarize this page/article at [url]"
          • Uses Playwright headless browser or httpx fallback
          • Returns actual web content, not training data

FIX P7  YouTube in browser_agent: removed the Invidious/Piped API path
        as the fallback. Now always uses DDG search with "site:youtube.com"
        query so it returns real results without needing external APIs that
        are frequently down.

FIX P8  "open chrome and X" / "go to [site]" routes to automation_agent
        PC sequence (retained from patch 2).
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
        self.vision_model = settings.QWEN_VISION_MODEL

    async def chat(self, system_prompt: str, user_message: str,
                   temperature: float = 0.7) -> str:
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_message},
            ],
            temperature=temperature,
        )
        return response.choices[0].message.content.strip()

    async def chat_with_image(self, system_prompt: str, user_message: str,
                               image_base64: str, temperature: float = 0.1) -> str:
        user_content = [
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}},
            {"type": "text", "text": user_message},
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
            logger.warning(f"[QwenClient] chat_with_image failed ({e}). Falling back.")
            return await self.chat(system_prompt, user_message, temperature)

    # =========================================================================
    # KEYWORD SETS  —  ordered from most-specific to least-specific
    # =========================================================================

    # ── MEMORY (always hard-route, checked first) ─────────────────────────────
    MEMORY_KEYWORDS = [
        "remember that", "remember my", "save that", "store that",
        "note that", "my preference is", "i prefer", "i use", "i like",
        "what do you know about me", "what have you remembered",
        "show my preferences", "list my preferences",
        "forget my", "forget that", "delete my preference",
        "clear my preferences", "clear all preferences",
        "recall my", "do you remember", "do you know my",
        "what is my", "what's my",
    ]

    # ── SCREENSHOT (always hard-route) ────────────────────────────────────────
    SCREENSHOT_KEYWORDS = [
        "take a screenshot", "take screenshot", "screenshot of my screen",
        "capture screen", "capture my screen", "screenshot now",
        "take a screen capture", "screen capture", "take a screenshoot",
    ]

    # ── CREATIVE WRITING → CHAT (never goes to task) ─────────────────────────
    # These should always be answered directly by Qwen, no agents needed.
    CREATIVE_CHAT_KEYWORDS = [
        "write me a haiku", "write a haiku", "write me a poem", "write a poem",
        "write me a song", "write a song", "write me a story", "write a short story",
        "write me a joke", "tell me a joke", "write me a limerick",
        "write a limerick", "write me a sonnet", "write a sonnet",
        "write a letter to", "write me a letter", "write an essay about",
        "write me an essay", "write a paragraph about", "compose a poem",
        "compose a song", "create a poem", "make up a story",
        "write me a rap", "write a rap about",
    ]

    # ── PC AUTOMATION: "open chrome and X" ───────────────────────────────────
    CHROME_AUTOMATION_KEYWORDS = [
        "open chrome and", "launch chrome and", "open browser and",
        "open firefox and", "open edge and", "open chrome to", "open chrome then",
    ]

    # ── PC NAVIGATION: "go to / open [site name]" on physical PC ─────────────
    PC_NAV_KEYWORDS = [
        "open youtube", "go to youtube", "open facebook", "go to facebook",
        "open reddit", "go to reddit", "open twitter", "go to twitter",
        "open instagram", "go to instagram", "open github", "go to github",
        "open netflix", "go to netflix", "open spotify", "go to spotify",
        "open linkedin", "go to linkedin", "open tiktok", "go to tiktok",
        "open amazon", "go to amazon", "open gmail", "go to gmail",
        "open stackoverflow", "go to stackoverflow",
    ]

    # ── BROWSER AGENT: real-time web, specific URLs, YouTube search ──────────
    # These need actual web access — NOT chat or research.
    BROWSER_KEYWORDS = [
        # YouTube search (results, not navigation)
        "search youtube for", "youtube for", "on youtube", "find on youtube",
        "youtube search", "youtube video", "youtube channel",
        # Specific URL scraping / summarizing
        "go to http", "go to www", "visit http", "navigate to http",
        "summarize the page at", "summarize the website", "summarize the article at",
        "read the article at", "scrape", "extract from",
        # Hacker News
        "hacker news", "hackernews", "ycombinator", "news.ycombinator",
        # Real-time data that MUST come from web
        "current price of", "price of bitcoin", "price of ethereum",
        "stock price", "current bitcoin", "current ethereum",
        "live score", "latest match",
        # Explicit web search
        "search google for", "google for", "google search",
        "search the web for", "search bing for",
        "search the internet for",
        "using browser agent", "use browser agent",
        "wikipedia", "wiki/",
    ]

    # ── RESEARCH AGENT: only when user explicitly wants research ─────────────
    # Identity: thesis writer, academic researcher, finds real sources with URLs.
    RESEARCH_KEYWORDS = [
        "research ", "do research on", "do some research",
        "find studies on", "find research on", "find sources on",
        "academic sources", "scientific sources", "find papers on",
        "literature review", "literature on", "bibliography",
        "cite sources", "with citations", "with references",
        "thesis on", "write a research paper", "research paper on",
        "peer reviewed", "peer-reviewed", "scholarly",
        "investigate ", "conduct research",
    ]

    # ── REAL-TIME (weather/time) → browser search ─────────────────────────────
    REALTIME_KEYWORDS = [
        "what time is it", "what's the time", "current time",
        "what time in ", "time in ", "time now in",
        "what day is it", "what's today", "today's date", "current date",
        "what date is it",
        "weather in ", "current weather", "what's the weather",
        "temperature in ", "forecast for",
    ]

    # ── CODE KEYWORDS → coding_agent ─────────────────────────────────────────
    CODE_KEYWORDS = [
        "write a program", "create a program", "make a program",
        "write a script", "create a script",
        "write code", "create code", "generate code",
        "code that", "program that", "script that",
        "write a python program", "write a python script",
        "write a c program", "write a c++ program", "write a java program",
        "write a function", "create a function",
        "write an algorithm", "implement a",
        "build a program", "build a script",
    ]

    # ── SITE URL MAP for PC navigation ───────────────────────────────────────
    SITE_URL_MAP = {
        "youtube":       "https://youtube.com",
        "facebook":      "https://facebook.com",
        "twitter":       "https://twitter.com",
        "instagram":     "https://instagram.com",
        "reddit":        "https://reddit.com",
        "github":        "https://github.com",
        "netflix":       "https://netflix.com",
        "amazon":        "https://amazon.com",
        "gmail":         "https://mail.google.com",
        "linkedin":      "https://linkedin.com",
        "tiktok":        "https://tiktok.com",
        "spotify":       "https://open.spotify.com",
        "stackoverflow": "https://stackoverflow.com",
    }

    # =========================================================================
    # ROUTING HELPERS
    # =========================================================================

    def _is_creative_chat(self, cmd_lc: str) -> bool:
        """Creative writing → always chat, never task."""
        return any(k in cmd_lc for k in self.CREATIVE_CHAT_KEYWORDS)

    def _is_memory_task(self, cmd_lc: str) -> bool:
        return any(k in cmd_lc for k in self.MEMORY_KEYWORDS)

    def _is_screenshot_task(self, cmd_lc: str) -> bool:
        return any(k in cmd_lc for k in self.SCREENSHOT_KEYWORDS)

    def _is_chrome_automation(self, cmd_lc: str) -> bool:
        return any(k in cmd_lc for k in self.CHROME_AUTOMATION_KEYWORDS)

    def _is_pc_nav(self, cmd_lc: str) -> bool:
        return any(k in cmd_lc for k in self.PC_NAV_KEYWORDS)

    def _is_browser_task(self, cmd_lc: str) -> bool:
        return any(k in cmd_lc for k in self.BROWSER_KEYWORDS)

    def _is_research_task(self, cmd_lc: str) -> bool:
        return any(k in cmd_lc for k in self.RESEARCH_KEYWORDS)

    def _is_realtime_task(self, cmd_lc: str) -> bool:
        return any(k in cmd_lc for k in self.REALTIME_KEYWORDS)

    def _is_code_task(self, cmd_lc: str) -> bool:
        return any(k in cmd_lc for k in self.CODE_KEYWORDS)

    def _is_open_app_command(self, cmd_lc: str) -> bool:
        OPEN_VERBS  = ["open ", "launch ", "start "]
        OFFICE_APPS = ["word", "excel", "powerpoint", "ppt",
                       "notepad", "notepad++", "chrome", "firefox", "edge"]
        starts_with_open = any(
            cmd_lc.startswith(v) or f" {v}" in cmd_lc for v in OPEN_VERBS
        )
        mentions_app = any(app in cmd_lc for app in OFFICE_APPS)
        has_chrome_action = self._is_chrome_automation(cmd_lc)
        has_pc_nav        = self._is_pc_nav(cmd_lc)
        return starts_with_open and mentions_app and not has_chrome_action and not has_pc_nav

    # =========================================================================
    # COMMAND CLASSIFIER
    # =========================================================================

    async def classify_command(self, command: str, user_context: str = "") -> str:
        cmd_lc = command.lower()

        # Hard-routes that are always "task"
        if self._is_memory_task(cmd_lc):      return "task"
        if self._is_screenshot_task(cmd_lc):  return "task"
        if self._is_chrome_automation(cmd_lc): return "task"
        if self._is_pc_nav(cmd_lc):           return "task"
        if self._is_code_task(cmd_lc):        return "task"
        if self._is_browser_task(cmd_lc):     return "task"
        if self._is_research_task(cmd_lc):    return "task"
        if self._is_realtime_task(cmd_lc):    return "task"

        # FIX P5: Creative writing is always CHAT — never route to agents
        if self._is_creative_chat(cmd_lc):    return "chat"

        # Ask Qwen to classify ambiguous commands
        ctx_block = f"\n\nUser context:\n{user_context}" if user_context else ""
        system = (
            "You are a command classifier for AetherAI.\n"
            "Classify the input as 'chat' or 'task'.\n\n"
            "'chat' = questions, explanations, definitions, math, translations, "
            "creative writing (poems, stories, jokes, haikus, songs, essays, letters), "
            "general knowledge, advice.\n"
            "'task' = creating files, computer control, writing CODE (programs/scripts), "
            "web browsing, scraping, multi-step workflows.\n\n"
            "Key rule: writing creative text (poems, stories, haikus) is 'chat', "
            "writing code (programs, scripts) is 'task'.\n\n"
            "Return ONLY: chat OR task"
        )
        result = await self.chat(system, command + ctx_block, temperature=0.0)
        return "chat" if "chat" in result.strip().lower() else "task"

    # =========================================================================
    # TASK PLANNER
    # =========================================================================

    async def plan_task(self, command: str, user_context: str = "") -> list[dict]:
        cmd_lc = command.lower()

        # ── Hard-routes (order matters) ───────────────────────────────────────

        if self._is_memory_task(cmd_lc):
            return [{"step":1, "agent":"memory_agent",
                     "description":f"Memory: {command}",
                     "parameters":{"query":command}}]

        if self._is_screenshot_task(cmd_lc):
            return [{"step":1, "agent":"automation_agent",
                     "description":"Take a screenshot",
                     "parameters":{"action":"screenshot_and_return"}}]

        if self._is_chrome_automation(cmd_lc):
            return self._build_chrome_automation_plan(command, cmd_lc)

        if self._is_pc_nav(cmd_lc):
            return self._build_pc_nav_plan(command, cmd_lc)

        # ── Research: explicit research keyword ───────────────────────────────
        if self._is_research_task(cmd_lc):
            return [{"step":1, "agent":"research_agent",
                     "description":f"Research: {command}",
                     "parameters":{"query":command}}]

        # ── Browser: real-time web, YouTube, specific URLs ────────────────────
        if self._is_browser_task(cmd_lc):
            return self._build_browser_plan(command, cmd_lc)

        # ── Real-time queries → browser search ───────────────────────────────
        if self._is_realtime_task(cmd_lc):
            return [{"step":1, "agent":"browser_agent",
                     "description":f"Real-time search: {command}",
                     "parameters":{"action":"search","query":command,"engine":"google"}}]

        # ── Code tasks → coding_agent ─────────────────────────────────────────
        if self._is_code_task(cmd_lc):
            lang = self._detect_language_hint(cmd_lc)
            return [{"step":1, "agent":"coding_agent",
                     "description":f"Write code: {command}",
                     "parameters":{"task":command,"language":lang}}]

        # ── Qwen LLM planner for everything else ──────────────────────────────
        is_open_app = self._is_open_app_command(cmd_lc)

        no_doc_rule = ""
        if is_open_app:
            no_doc_rule = (
                "\n⚠️ CRITICAL: This is 'open [app]'. "
                "Use automation_agent ONLY. NEVER use document_agent.\n"
            )

        ctx_block = f"\n\nUser preferences:\n{user_context}" if user_context else ""

        system_prompt = (
            "You are AetherAI's task planner. "
            "Return ONLY a valid JSON array. No markdown fences.\n\n"
            + no_doc_rule +
            "Each step: {step, agent, description, parameters}\n\n"
            "AGENTS:\n"
            "research_agent  — deep multi-source research with citations\n"
            '  {"query":"..."}\n\n'
            "browser_agent   — real-time web, YouTube, scrape URLs\n"
            '  {"action":"search","query":"...","engine":"google"}\n'
            '  {"action":"youtube","query":"..."}\n'
            '  {"action":"scrape","url":"https://..."}\n\n'
            "document_agent  — create .pptx/.docx/.xlsx files to download\n"
            '  {"type":"presentation"|"document"|"spreadsheet","topic":"..."}\n'
            "  TERMINAL — no steps after it.\n\n"
            "coding_agent    — write and save source code files\n"
            '  {"task":"...","language":"python|js|..."}\n'
            "  TERMINAL — no steps after it.\n\n"
            "automation_agent — control the physical PC\n"
            '  {"action":"open_app","parameters":{"app":"chrome|notepad|..."}}\n'
            '  {"action":"type","parameters":{"text":"__GENERATED_CONTENT__"}}\n'
            '  {"action":"hotkey","parameters":{"keys":["ctrl","s"]}}\n'
            '  {"action":"wait","parameters":{"ms":2000}}\n\n'
            "memory_agent    — save/recall/forget preferences\n"
            '  {"query":"..."}\n\n'
            "RULES:\n"
            "1. Document/presentation creation → document_agent (SINGLE step)\n"
            "2. Code writing → coding_agent (SINGLE step)\n"
            "3. PC control → automation_agent\n"
            "4. Explicit research → research_agent\n"
            "5. Web scraping / YouTube → browser_agent\n"
        )

        raw = await self.chat(system_prompt,
                              f'Plan: "{command}"{ctx_block}',
                              temperature=0.2)
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

        try:
            plan = json.loads(raw)
            if not isinstance(plan, list):
                raise ValueError
            if is_open_app:
                plan = [s for s in plan if s.get("agent") != "document_agent"]
                for idx, s in enumerate(plan, 1):
                    s["step"] = idx
            return self._strip_trailing_steps(plan)
        except (json.JSONDecodeError, ValueError):
            return [{"step":1, "agent":"research_agent",
                     "description":f"Process: {command}",
                     "parameters":{"query":command}}]

    # =========================================================================
    # BROWSER PLAN BUILDER
    # =========================================================================

    def _build_browser_plan(self, command: str, cmd_lc: str) -> list[dict]:

        # YouTube search (returns video results)
        if any(k in cmd_lc for k in
               ["search youtube for", "youtube for", "on youtube",
                "find on youtube", "youtube search", "youtube video"]):
            query = cmd_lc
            for pat in ["search youtube for", "youtube for", "on youtube",
                        "find on youtube", "youtube search"]:
                query = re.sub(rf".*{re.escape(pat)}\s*", "", query,
                               flags=re.IGNORECASE).strip()
            query = query or command
            return [{"step":1, "agent":"browser_agent",
                     "description":f"Search YouTube: {query}",
                     "parameters":{"action":"youtube", "query":query}}]

        # Hacker News
        if any(k in cmd_lc for k in
               ["hacker news","hackernews","ycombinator","news.ycombinator"]):
            return [{"step":1, "agent":"browser_agent",
                     "description":"Fetch Hacker News top stories",
                     "parameters":{"action":"workflow","goal":command}}]

        # Wikipedia
        if "wikipedia" in cmd_lc or "wiki/" in cmd_lc:
            url_m = re.search(r"(https?://[^\s]+|wikipedia\.org/wiki/[^\s]+)", cmd_lc)
            if url_m:
                url = url_m.group(1)
                if not url.startswith("http"): url = "https://" + url
                return [{"step":1, "agent":"browser_agent",
                         "description":f"Scrape Wikipedia: {url}",
                         "parameters":{"action":"scrape","url":url}}]
            return [{"step":1, "agent":"browser_agent",
                     "description":f"Wikipedia search: {command}",
                     "parameters":{"action":"search","query":command,"engine":"google"}}]

        # Explicit URL
        url_m = re.search(r"https?://[^\s]+", command)
        if url_m:
            url = url_m.group(0)
            return [{"step":1, "agent":"browser_agent",
                     "description":f"Scrape: {url}",
                     "parameters":{"action":"scrape","url":url}}]

        # Real-time price / stock
        if any(k in cmd_lc for k in
               ["price of", "current price", "bitcoin price", "ethereum price",
                "stock price", "live score"]):
            return [{"step":1, "agent":"browser_agent",
                     "description":f"Real-time search: {command}",
                     "parameters":{"action":"search","query":command,"engine":"google"}}]

        # Explicit Google/web search
        if any(k in cmd_lc for k in
               ["search google for","google for","google search",
                "search the web for","search bing for","search the internet for"]):
            query = re.sub(
                r".*(search google for|google for|search the web for|"
                r"search bing for|google search|search the internet for)\s*",
                "", cmd_lc, flags=re.IGNORECASE
            ).strip() or command
            engine = "bing" if "bing" in cmd_lc else "google"
            return [{"step":1, "agent":"browser_agent",
                     "description":f"Web search: {query}",
                     "parameters":{"action":"search","query":query,"engine":engine}}]

        # Summarize/scrape a specific URL in the command
        dest_m = re.search(r"(?:go to|open|visit|at)\s+([\w\-\.]+\.[\w]{2,}[^\s]*)",
                           cmd_lc)
        if dest_m:
            url = dest_m.group(1)
            if not url.startswith("http"): url = "https://" + url
            return [{"step":1, "agent":"browser_agent",
                     "description":f"Scrape: {url}",
                     "parameters":{"action":"scrape","url":url}}]

        # Default: browser search
        return [{"step":1, "agent":"browser_agent",
                 "description":f"Web search: {command}",
                 "parameters":{"action":"search","query":command,"engine":"google"}}]

    # =========================================================================
    # PC AUTOMATION PLAN BUILDERS
    # =========================================================================

    def _build_chrome_automation_plan(self, command: str, cmd_lc: str) -> list[dict]:
        intent = cmd_lc
        for prefix in self.CHROME_AUTOMATION_KEYWORDS:
            intent = re.sub(rf".*{re.escape(prefix)}\s*", "", intent,
                            flags=re.IGNORECASE).strip()
        url = self._extract_url_from_intent(intent)
        return [
            {"step":1,"agent":"automation_agent",
             "description":"Open Chrome on the PC",
             "parameters":{"action":"open_app","parameters":{"app":"chrome"}}},
            {"step":2,"agent":"automation_agent",
             "description":"Wait for Chrome to load",
             "parameters":{"action":"wait","parameters":{"ms":2500}}},
            {"step":3,"agent":"automation_agent",
             "description":f"Navigate to: {url}",
             "parameters":{"action":"type","parameters":{"text":url}}},
            {"step":4,"agent":"automation_agent",
             "description":"Press Enter",
             "parameters":{"action":"hotkey","parameters":{"keys":["enter"]}}},
        ]

    def _build_pc_nav_plan(self, command: str, cmd_lc: str) -> list[dict]:
        url = "https://google.com"
        for site, site_url in self.SITE_URL_MAP.items():
            if site in cmd_lc:
                url = site_url
                break
        return [
            {"step":1,"agent":"automation_agent",
             "description":"Open Chrome on the PC",
             "parameters":{"action":"open_app","parameters":{"app":"chrome"}}},
            {"step":2,"agent":"automation_agent",
             "description":"Wait for Chrome to load",
             "parameters":{"action":"wait","parameters":{"ms":2500}}},
            {"step":3,"agent":"automation_agent",
             "description":f"Navigate to {url}",
             "parameters":{"action":"type","parameters":{"text":url}}},
            {"step":4,"agent":"automation_agent",
             "description":"Press Enter",
             "parameters":{"action":"hotkey","parameters":{"keys":["enter"]}}},
        ]

    def _extract_url_from_intent(self, intent: str) -> str:
        url_m = re.search(r"https?://\S+", intent)
        if url_m: return url_m.group(0)
        for site, url in self.SITE_URL_MAP.items():
            if re.search(rf"\b{site}\b", intent, re.IGNORECASE):
                return url
        search_m = re.search(r"(?:search(?: for)?|look up|find)\s+(.+)",
                              intent, re.IGNORECASE)
        if search_m: return search_m.group(1).strip()
        domain_m = re.search(r"[\w\-]+\.(com|org|net|io|co|app|dev)\b", intent)
        if domain_m: return "https://" + domain_m.group(0)
        return intent or "https://google.com"

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _detect_language_hint(self, cmd_lc: str) -> str:
        if "python" in cmd_lc:     return "python"
        if "javascript" in cmd_lc or " js " in cmd_lc: return "javascript"
        if "typescript" in cmd_lc: return "typescript"
        if "c++" in cmd_lc:        return "cpp"
        if " java " in cmd_lc:     return "java"
        if "rust" in cmd_lc:       return "rust"
        if "golang" in cmd_lc or " go " in cmd_lc: return "go"
        if " c " in cmd_lc and "program" in cmd_lc: return "c"
        return ""

    def _strip_trailing_steps(self, plan: list) -> list:
        TERMINAL = {"document_agent", "coding_agent"}
        if not plan: return plan
        last = -1
        for i, s in enumerate(plan):
            if s.get("agent") in TERMINAL:
                last = i
        if last < 0: return plan
        trailing = plan[last + 1:]
        if not trailing: return plan
        if all(s.get("agent") == "research_agent" for s in trailing):
            return plan[:last + 1]
        return plan

    # =========================================================================
    # CONTENT / UTILITIES
    # =========================================================================

    async def generate_content(self, command: str, content_type: str = "text") -> str:
        system = (
            "You are a creative writing assistant. "
            "Produce ONLY the requested content. "
            "No preamble, no explanation. Begin immediately."
        )
        clean = re.sub(
            r"\b(open|launch|use)\s+(notepad\+\+|notepad|word|a file)\s*(and|then|to)?\s*",
            "", command, flags=re.IGNORECASE
        ).strip() or command
        return await self.chat(system, f"Write: {clean}", temperature=0.7)

    async def summarize(self, content: str, context: str = "") -> str:
        system = "You are a concise summarizer. Summarize clearly and briefly."
        user   = f"{context}\n\nContent:\n{content}" if context else content
        return await self.chat(system, user, temperature=0.5)

    async def answer(self, question: str, context: str = "",
                     user_context: str = "") -> str:
        system = (
            "You are AetherAI, a personal AI agent built by Patrick Perez, "
            "a 26-year-old software engineer from the Philippines. "
            "Answer clearly and directly. "
            "For creative writing requests, produce the creative content directly — "
            "do not explain or add preamble."
        )
        parts = []
        if user_context: parts.append(f"User facts:\n{user_context}")
        if context:      parts.append(f"Context:\n{context}")
        parts.append(f"Request: {question}")
        return await self.chat(system, "\n\n".join(parts))
