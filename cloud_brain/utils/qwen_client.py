"""
AetherAI — Qwen API Client  (Stage 5 — patch 4)

Fixes applied
─────────────
FIX 1  answer() system prompt now explicitly requests detailed, thorough
       responses. No more artificially short answers.

FIX 2  summarize() now takes an optional `source` parameter. When source
       is "web", the prompt explicitly tells Qwen: "This content was just
       fetched from the live web. Use it. Do NOT say 'as of my knowledge
       cutoff' or 'I cannot access real-time data'."

FIX 3  Agent routing tightened:
       - Chat: general Q&A, creative writing, math, translations, advice
       - Research: ONLY when "research" keyword or academic intent present
       - Browser: specific URLs, YouTube, real-time data, web scraping
       - The fallback LLM classifier now has an explicit example list so
         borderline cases go to chat, not research.
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

    # ── Core LLM calls ────────────────────────────────────────────────────────

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

    # ── FIX 1: Rich answer() ──────────────────────────────────────────────────

    async def answer(self, question: str, context: str = "",
                     user_context: str = "") -> str:
        """
        Direct Qwen answer for chat mode.
        Produces detailed, well-structured responses — not artificially short.
        """
        system = (
            "You are AetherAI, a highly capable personal AI assistant built by "
            "Patrick Perez, a 26-year-old software engineer from the Philippines.\n\n"
            "RESPONSE QUALITY RULES:\n"
            "- Give thorough, detailed answers. Do not truncate or oversimplify.\n"
            "- For factual questions: provide complete explanations with context.\n"
            "- For creative writing (poems, haikus, stories, jokes): produce the "
            "full creative piece immediately, then optionally add brief context.\n"
            "- For math: show the working, then give the answer.\n"
            "- For translations: give the translation and explain pronunciation "
            "or nuance where helpful.\n"
            "- For health/science: give a comprehensive response with specific "
            "details, mechanisms, and practical information.\n"
            "- Use markdown formatting (bold headings, bullet points) for "
            "structured topics. Write naturally for conversational questions.\n"
            "- Never say 'I cannot access real-time data' — just answer from "
            "your knowledge and note when something may have changed recently."
        )
        parts = []
        if user_context:
            parts.append(f"Facts about this user:\n{user_context}")
        if context:
            parts.append(f"Additional context:\n{context}")
        parts.append(f"Request: {question}")
        return await self.chat(system, "\n\n".join(parts), temperature=0.7)

    # ── FIX 2: Web-aware summarize() ─────────────────────────────────────────

    async def summarize(self, content: str, context: str = "",
                        source: str = "") -> str:
        """
        Summarize content.
        source="web" tells Qwen this content was just fetched live — prevents
        it from falling back to training data or saying knowledge-cutoff phrases.
        """
        if source == "web":
            system = (
                "You are an information analyst. You have been given content that was "
                "JUST fetched live from the internet RIGHT NOW.\n\n"
                "CRITICAL RULES:\n"
                "- This is real, current web content. Use it.\n"
                "- NEVER say 'as of my knowledge cutoff', 'I cannot access real-time "
                "data', or any similar phrase. That would be wrong — you DO have "
                "the current data in the content provided.\n"
                "- Base your summary ENTIRELY on the provided content.\n"
                "- Be thorough and detailed. Extract all key facts, numbers, names, "
                "and specific information present in the content.\n"
                "- Use clear structure: headings, bullet points where appropriate.\n"
                "- If the content contains prices, dates, or statistics, include them."
            )
        else:
            system = (
                "You are a thorough content analyst. "
                "Summarize the provided content in detail — include all key facts, "
                "specific figures, names, and important details. "
                "Use clear structure with headings and bullet points where appropriate. "
                "Do not truncate or oversimplify."
            )
        user = f"{context}\n\nContent to summarize:\n{content}" if context else content
        return await self.chat(system, user, temperature=0.4)

    # =========================================================================
    # KEYWORD SETS
    # =========================================================================

    MEMORY_KEYWORDS = [
        "remember that", "remember my", "save that", "store that",
        "note that", "my preference is", "i prefer", "i use", "i like",
        "what do you know about me", "what have you remembered",
        "show my preferences", "list my preferences",
        "forget my", "forget that", "delete my preference",
        "clear my preferences", "clear all preferences",
        "recall my", "do you remember my", "do you know my",
        "what is my ", "what's my ",
    ]

    SCREENSHOT_KEYWORDS = [
        "take a screenshot", "take screenshot", "screenshot of my screen",
        "capture screen", "capture my screen", "screenshot now",
        "take a screen capture", "screen capture", "take a screenshoot",
    ]

    # Creative writing → always CHAT, never task
    CREATIVE_CHAT_KEYWORDS = [
        "write me a haiku", "write a haiku",
        "write me a poem", "write a poem", "compose a poem",
        "write me a song", "write a song", "compose a song",
        "write me a story", "write a short story", "tell me a story",
        "write me a joke", "tell me a joke", "tell me a riddle",
        "write me a limerick", "write a limerick",
        "write me a sonnet", "write a sonnet",
        "write a letter to ", "write me a letter",
        "write an essay about", "write me an essay",
        "write a paragraph about",
        "write me a rap", "write a rap about",
        "write me a caption",
    ]

    # "open chrome and X" or "launch browser and X" → automation sequence on PC
    CHROME_AUTOMATION_KEYWORDS = [
        "open chrome and", "launch chrome and", "open browser and",
        "open firefox and", "open edge and", "open chrome to", "open chrome then",
    ]

    # "go to youtube" / "open facebook" on physical PC
    PC_NAV_KEYWORDS = [
        "open youtube", "go to youtube",
        "open facebook", "go to facebook",
        "open reddit", "go to reddit",
        "open twitter", "go to twitter",
        "open instagram", "go to instagram",
        "open github", "go to github",
        "open netflix", "go to netflix",
        "open spotify", "go to spotify",
        "open linkedin", "go to linkedin",
        "open tiktok", "go to tiktok",
        "open amazon", "go to amazon",
        "open gmail", "go to gmail",
        "open stackoverflow", "go to stackoverflow",
    ]

    # Browser agent: specific URLs, YouTube, HN, real-time web
    BROWSER_KEYWORDS = [
        # YouTube searches
        "search youtube for", "on youtube", "find on youtube",
        "youtube search", "youtube video", "youtube channel",
        # URL scraping / summarizing
        "go to http", "go to www", "visit http", "navigate to http",
        "summarize the page at", "summarize the website",
        "summarize the article at", "read the article at",
        "scrape ", "extract from ",
        # Hacker News
        "hacker news", "hackernews", "ycombinator", "news.ycombinator",
        # Real-time financial/sports
        "current price of", "price of bitcoin", "price of ethereum",
        "btc price", "eth price", "stock price", "live score",
        # Explicit web search
        "search google for", "google for", "google search for",
        "search the web for", "search bing for", "search the internet for",
        "look up on google",
        # Wikipedia
        "wikipedia", "wiki/",
    ]

    # Research agent: ONLY explicit research intent
    RESEARCH_KEYWORDS = [
        "research ", "do research on", "do some research",
        "find studies on", "find research on", "find sources on",
        "academic sources", "scientific sources", "find papers on",
        "literature review", "with citations", "with references",
        "thesis on", "research paper on", "write a research paper",
        "peer reviewed", "scholarly sources", "investigate ",
    ]

    # Code writing → coding_agent
    CODE_KEYWORDS = [
        "write a program", "create a program", "make a program",
        "write a script", "create a script",
        "write code", "create code", "generate code",
        "code that", "program that", "script that",
        "write a python program", "write a python script",
        "write a c program", "write a c++ program",
        "write a java program", "write a javascript",
        "write a function", "create a function",
        "write an algorithm", "implement a ",
        "build a program", "build a script",
    ]

    # Real-time → browser search
    REALTIME_KEYWORDS = [
        "what time is it", "what's the time", "current time",
        "what time in ", "time in ", "time now in",
        "what day is it", "today's date", "current date",
        "weather in ", "current weather", "what's the weather",
        "temperature in ", "forecast for",
    ]

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

    # ── Routing helpers ───────────────────────────────────────────────────────

    def _is_creative_chat(self, cmd_lc: str) -> bool:
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
        starts = any(cmd_lc.startswith(v) or f" {v}" in cmd_lc for v in OPEN_VERBS)
        app    = any(a in cmd_lc for a in OFFICE_APPS)
        return starts and app and not self._is_chrome_automation(cmd_lc) \
               and not self._is_pc_nav(cmd_lc)

    # =========================================================================
    # COMMAND CLASSIFIER
    # =========================================================================

    async def classify_command(self, command: str, user_context: str = "") -> str:
        cmd_lc = command.lower()

        # Hard task routes
        if self._is_memory_task(cmd_lc):       return "task"
        if self._is_screenshot_task(cmd_lc):   return "task"
        if self._is_chrome_automation(cmd_lc): return "task"
        if self._is_pc_nav(cmd_lc):            return "task"
        if self._is_code_task(cmd_lc):         return "task"
        if self._is_browser_task(cmd_lc):      return "task"
        if self._is_research_task(cmd_lc):     return "task"
        if self._is_realtime_task(cmd_lc):     return "task"

        # Creative writing → always chat
        if self._is_creative_chat(cmd_lc):     return "chat"

        # FIX 3: Explicit examples so Qwen classifies borderline cases correctly
        ctx_block = f"\n\nUser context:\n{user_context}" if user_context else ""
        system = (
            "You are a command classifier for AetherAI.\n"
            "Classify the input as exactly 'chat' or 'task'.\n\n"
            "'chat' examples:\n"
            "  - what is quantum computing\n"
            "  - who is Elon Musk\n"
            "  - what are the health benefits of green tea\n"
            "  - translate hello to Spanish\n"
            "  - write me a poem about the ocean\n"
            "  - what is 1847 divided by 13\n"
            "  - explain how photosynthesis works\n"
            "  - what happened during World War 2\n"
            "  - give me advice on learning programming\n\n"
            "'task' examples:\n"
            "  - create a PowerPoint presentation\n"
            "  - write a Python script\n"
            "  - open notepad and write a letter\n"
            "  - go to wikipedia and summarize it\n"
            "  - take a screenshot\n"
            "  - search youtube for tutorials\n\n"
            "Key rule: If it's a question, explanation, creative writing, "
            "translation, or general knowledge → 'chat'. "
            "If it requires creating files, running code, controlling PC, "
            "or web browsing → 'task'.\n\n"
            "Return ONLY the single word: chat OR task"
        )
        result = await self.chat(system, command + ctx_block, temperature=0.0)
        return "chat" if "chat" in result.strip().lower() else "task"

    # =========================================================================
    # TASK PLANNER
    # =========================================================================

    async def plan_task(self, command: str, user_context: str = "") -> list[dict]:
        cmd_lc = command.lower()

        # Hard-routes in priority order
        if self._is_memory_task(cmd_lc):
            return [{"step":1,"agent":"memory_agent",
                     "description":f"Memory: {command}",
                     "parameters":{"query":command}}]

        if self._is_screenshot_task(cmd_lc):
            return [{"step":1,"agent":"automation_agent",
                     "description":"Take a screenshot",
                     "parameters":{"action":"screenshot_and_return"}}]

        if self._is_chrome_automation(cmd_lc):
            return self._build_chrome_automation_plan(command, cmd_lc)

        if self._is_pc_nav(cmd_lc):
            return self._build_pc_nav_plan(command, cmd_lc)

        # Research: explicit "research" keyword only
        if self._is_research_task(cmd_lc):
            return [{"step":1,"agent":"research_agent",
                     "description":f"Research: {command}",
                     "parameters":{"query":command}}]

        # Browser: specific URLs, YouTube, real-time, explicit web search
        if self._is_browser_task(cmd_lc):
            return self._build_browser_plan(command, cmd_lc)

        # Real-time queries
        if self._is_realtime_task(cmd_lc):
            return [{"step":1,"agent":"browser_agent",
                     "description":f"Real-time: {command}",
                     "parameters":{"action":"search","query":command,"engine":"google"}}]

        # Code tasks
        if self._is_code_task(cmd_lc):
            lang = self._detect_language_hint(cmd_lc)
            return [{"step":1,"agent":"coding_agent",
                     "description":f"Write code: {command}",
                     "parameters":{"task":command,"language":lang}}]

        # Qwen LLM planner for everything else
        is_open_app = self._is_open_app_command(cmd_lc)
        no_doc_rule = (
            "\n⚠️ CRITICAL: 'open [app]' command — use automation_agent ONLY, "
            "NEVER document_agent.\n"
        ) if is_open_app else ""

        ctx_block = f"\n\nUser preferences:\n{user_context}" if user_context else ""

        system_prompt = (
            "You are AetherAI's task planner. "
            "Return ONLY a valid JSON array. No markdown.\n\n"
            + no_doc_rule +
            "AGENTS:\n"
            "research_agent  — academic research with real sources\n"
            '  {"query":"..."}\n\n'
            "browser_agent   — scrape URLs, YouTube, real-time web\n"
            '  {"action":"search","query":"...","engine":"google"}\n'
            '  {"action":"youtube","query":"..."}\n'
            '  {"action":"scrape","url":"https://..."}\n\n'
            "document_agent  — create .pptx/.docx/.xlsx\n"
            '  {"type":"presentation"|"document"|"spreadsheet","topic":"..."}\n'
            "  TERMINAL step.\n\n"
            "coding_agent    — write and save code files\n"
            '  {"task":"...","language":"python|js|..."}\n'
            "  TERMINAL step.\n\n"
            "automation_agent — control the physical PC\n"
            '  {"action":"open_app","parameters":{"app":"..."}}\n'
            '  {"action":"type","parameters":{"text":"__GENERATED_CONTENT__"}}\n'
            '  {"action":"hotkey","parameters":{"keys":["ctrl","s"]}}\n'
            '  {"action":"wait","parameters":{"ms":2000}}\n\n'
            "memory_agent    — save/recall preferences\n"
            '  {"query":"..."}\n\n'
            "Each step: {step, agent, description, parameters}"
        )

        raw = await self.chat(system_prompt,
                              f'Plan: "{command}"{ctx_block}',
                              temperature=0.2)
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

        try:
            plan = json.loads(raw)
            if not isinstance(plan, list): raise ValueError
            if is_open_app:
                plan = [s for s in plan if s.get("agent") != "document_agent"]
                for idx, s in enumerate(plan, 1): s["step"] = idx
            return self._strip_trailing_steps(plan)
        except (json.JSONDecodeError, ValueError):
            return [{"step":1,"agent":"research_agent",
                     "description":f"Process: {command}",
                     "parameters":{"query":command}}]

    # =========================================================================
    # BROWSER PLAN BUILDER
    # =========================================================================

    def _build_browser_plan(self, command: str, cmd_lc: str) -> list[dict]:

        # YouTube search
        if any(k in cmd_lc for k in
               ["search youtube for","on youtube","find on youtube",
                "youtube search","youtube video"]):
            query = cmd_lc
            for pat in ["search youtube for","on youtube","find on youtube","youtube search"]:
                query = re.sub(rf".*{re.escape(pat)}\s*", "", query,
                               flags=re.IGNORECASE).strip()
            return [{"step":1,"agent":"browser_agent",
                     "description":f"Search YouTube: {query or command}",
                     "parameters":{"action":"youtube","query":query or command}}]

        # Hacker News
        if any(k in cmd_lc for k in
               ["hacker news","hackernews","ycombinator","news.ycombinator"]):
            return [{"step":1,"agent":"browser_agent",
                     "description":"Hacker News top stories",
                     "parameters":{"action":"workflow","goal":command}}]

        # Wikipedia
        if "wikipedia" in cmd_lc or "wiki/" in cmd_lc:
            url_m = re.search(r"(https?://[^\s]+|wikipedia\.org/wiki/[^\s]+)", cmd_lc)
            if url_m:
                url = url_m.group(1)
                if not url.startswith("http"): url = "https://" + url
                return [{"step":1,"agent":"browser_agent",
                         "description":f"Scrape Wikipedia: {url}",
                         "parameters":{"action":"scrape","url":url}}]
            return [{"step":1,"agent":"browser_agent",
                     "description":f"Wikipedia search: {command}",
                     "parameters":{"action":"search","query":command,"engine":"google"}}]

        # Explicit URL in command
        url_m = re.search(r"https?://[^\s]+", command)
        if url_m:
            url = url_m.group(0)
            return [{"step":1,"agent":"browser_agent",
                     "description":f"Scrape: {url}",
                     "parameters":{"action":"scrape","url":url}}]

        # "go to [domain]" or "summarize the article at [domain]"
        dest_m = re.search(
            r"(?:go to|open|visit|at|summarize.*?at)\s+([\w\-\.]+\.[\w]{2,}[^\s]*)",
            cmd_lc
        )
        if dest_m:
            url = dest_m.group(1)
            if not url.startswith("http"): url = "https://" + url
            return [{"step":1,"agent":"browser_agent",
                     "description":f"Scrape: {url}",
                     "parameters":{"action":"scrape","url":url}}]

        # Explicit web/Google search
        if any(k in cmd_lc for k in
               ["search google for","google for","google search",
                "search the web for","search bing for",
                "search the internet for","look up on google"]):
            query = re.sub(
                r".*(search google for|google for|search the web for|"
                r"search bing for|google search for?|search the internet for|"
                r"look up on google)\s*",
                "", cmd_lc, flags=re.IGNORECASE
            ).strip() or command
            engine = "bing" if "bing" in cmd_lc else "google"
            return [{"step":1,"agent":"browser_agent",
                     "description":f"Web search: {query}",
                     "parameters":{"action":"search","query":query,"engine":engine}}]

        # Real-time price / live data
        if any(k in cmd_lc for k in
               ["current price","price of bitcoin","price of ethereum",
                "btc price","eth price","stock price","live score"]):
            return [{"step":1,"agent":"browser_agent",
                     "description":f"Real-time data: {command}",
                     "parameters":{"action":"search","query":command,"engine":"google"}}]

        # Default: web search
        return [{"step":1,"agent":"browser_agent",
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
             "description":"Open Chrome",
             "parameters":{"action":"open_app","parameters":{"app":"chrome"}}},
            {"step":2,"agent":"automation_agent",
             "description":"Wait for Chrome",
             "parameters":{"action":"wait","parameters":{"ms":2500}}},
            {"step":3,"agent":"automation_agent",
             "description":f"Navigate to: {url}",
             "parameters":{"action":"type","parameters":{"text":url}}},
            {"step":4,"agent":"automation_agent",
             "description":"Go",
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
             "description":"Open Chrome",
             "parameters":{"action":"open_app","parameters":{"app":"chrome"}}},
            {"step":2,"agent":"automation_agent",
             "description":"Wait for Chrome",
             "parameters":{"action":"wait","parameters":{"ms":2500}}},
            {"step":3,"agent":"automation_agent",
             "description":f"Navigate to {url}",
             "parameters":{"action":"type","parameters":{"text":url}}},
            {"step":4,"agent":"automation_agent",
             "description":"Go",
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
        if "javascript" in cmd_lc: return "javascript"
        if "typescript" in cmd_lc: return "typescript"
        if "c++" in cmd_lc:        return "cpp"
        if " java " in cmd_lc:     return "java"
        if "rust" in cmd_lc:       return "rust"
        if "golang" in cmd_lc:     return "go"
        if " c " in cmd_lc and "program" in cmd_lc: return "c"
        return ""

    def _strip_trailing_steps(self, plan: list) -> list:
        TERMINAL = {"document_agent", "coding_agent"}
        if not plan: return plan
        last = -1
        for i, s in enumerate(plan):
            if s.get("agent") in TERMINAL: last = i
        if last < 0: return plan
        trailing = plan[last + 1:]
        if not trailing: return plan
        if all(s.get("agent") == "research_agent" for s in trailing):
            return plan[:last + 1]
        return plan

    async def generate_content(self, command: str, content_type: str = "text") -> str:
        system = (
            "You are a creative writing assistant. "
            "Produce ONLY the requested content. No preamble. Begin immediately."
        )
        clean = re.sub(
            r"\b(open|launch|use)\s+(notepad\+\+|notepad|word|a file)\s*(and|then|to)?\s*",
            "", command, flags=re.IGNORECASE
        ).strip() or command
        return await self.chat(system, f"Write: {clean}", temperature=0.7)
