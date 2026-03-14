"""
AetherAI — Qwen API Client  (Stage 5 — patch 8)

Fixes
─────
FIX 1  "open word and write a letter" was going to CHAT because
       _is_creative_chat ("write a letter to") fired before
       _is_open_app_command. Added open_app check FIRST in
       classify_command so it correctly routes to task/automation.

FIX 2  Notepad loop: _build_open_app_write_plan now hard-routes
       "open [app] and write/type X" commands, producing a clean
       sequence that never generates both open_app + new_file.

FIX 3  Chrome navigation wait reduced from 2500ms → 1000ms.

FIX 4  Calculator keywords broadened so "calculate X" works without
       needing "open calculator and" prefix.
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

    async def answer(self, question: str, context: str = "",
                     user_context: str = "") -> str:
        system = (
            "You are AetherAI, a highly capable personal AI assistant built by "
            "Patrick Perez, a 26-year-old software engineer from the Philippines.\n\n"
            "RESPONSE QUALITY RULES:\n"
            "- Give thorough, detailed answers. Do not truncate or oversimplify.\n"
            "- For creative writing: produce the full piece immediately.\n"
            "- For math: show working then give the answer.\n"
            "- For translations: give translation and pronunciation.\n"
            "- For health/science: give comprehensive detail.\n"
            "- Use markdown for structured topics.\n"
            "- Never say 'I cannot access real-time data'."
        )
        parts = []
        if user_context: parts.append(f"Facts about this user:\n{user_context}")
        if context:      parts.append(f"Context:\n{context}")
        parts.append(f"Request: {question}")
        return await self.chat(system, "\n\n".join(parts), temperature=0.7)

    async def summarize(self, content: str, context: str = "",
                        source: str = "") -> str:
        if source == "web":
            system = (
                "You are an information analyst. Content was JUST fetched live from the web.\n"
                "NEVER say 'as of my knowledge cutoff' or 'I cannot access real-time data'.\n"
                "Base your summary ENTIRELY on the provided content.\n"
                "Be thorough — include all key facts, numbers, names, and specific details.\n"
                "Use clear structure with headings and bullet points."
            )
        else:
            system = (
                "You are a thorough content analyst. "
                "Summarize in detail — include all key facts and important details. "
                "Use clear structure. Do not truncate."
            )
        user = f"{context}\n\nContent:\n{content}" if context else content
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
    # FIX 1: These are creative chat ONLY when there's no app open command
    # They're checked with _is_open_app_command guard below
    CREATIVE_CHAT_KEYWORDS = [
        "write me a haiku", "write a haiku",
        "write me a poem", "write a poem", "compose a poem",
        "write me a song", "write a song",
        "write me a story", "write a short story", "tell me a story",
        "write me a joke", "tell me a joke", "tell me a riddle",
        "write me a limerick", "write a limerick",
        "write me a rap", "write a rap about",
        # Letters/essays only count as creative if no app is mentioned
        "write me a letter", "write an essay about", "write me an essay",
        "write a paragraph about",
    ]
    # FIX 2: "open [app] and write/type X" keywords — always task
    APP_WRITE_KEYWORDS = [
        "open notepad and write", "open notepad and type",
        "open notepad++ and write", "open notepad++ and type",
        "open word and write", "open word and type",
        "open word and create", "write a letter in word",
        "open excel and", "open powerpoint and",
        "open notepad and", "notepad and write", "notepad and type",
    ]
    CHROME_AUTOMATION_KEYWORDS = [
        "open chrome and", "launch chrome and", "open browser and",
        "open firefox and", "open edge and", "open chrome to", "open chrome then",
    ]
    PC_NAV_KEYWORDS = [
        "open youtube", "go to youtube", "open facebook", "go to facebook",
        "open reddit", "go to reddit", "open twitter", "go to twitter",
        "open instagram", "go to instagram", "open github", "go to github",
        "open netflix", "go to netflix", "open spotify", "go to spotify",
        "open linkedin", "go to linkedin", "open tiktok", "go to tiktok",
        "open amazon", "go to amazon", "open gmail", "go to gmail",
        "open stackoverflow", "go to stackoverflow",
    ]
    BROWSER_KEYWORDS = [
        "search youtube for", "on youtube", "find on youtube",
        "youtube search", "youtube video", "youtube channel",
        "go to http", "go to www", "visit http", "navigate to http",
        "summarize the page at", "summarize the website",
        "summarize the article at", "read the article at",
        "scrape ", "extract from ",
        "hacker news", "hackernews", "ycombinator", "news.ycombinator",
        "search google for", "google for", "google search for",
        "search the web for", "search bing for", "search the internet for",
        "look up on google", "wikipedia", "wiki/",
    ]
    RESEARCH_KEYWORDS = [
        "research ", "do research on", "do some research",
        "find studies on", "find research on", "find sources on",
        "academic sources", "scientific sources", "find papers on",
        "literature review", "with citations", "with references",
        "thesis on", "research paper on", "write a research paper",
        "peer reviewed", "scholarly sources", "investigate ",
    ]
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
    WEATHER_KEYWORDS = [
        "weather in ", "weather for ", "weather today", "weather tomorrow",
        "what's the weather", "what is the weather",
        "temperature in ", "temperature today",
        "will it rain", "is it raining", "forecast for ", "forecast in ",
        "how hot is ", "how cold is ", "humidity in ",
        "is it sunny", "is it cloudy", "is it snowing",
    ]
    CRYPTO_KEYWORDS = [
        "price of bitcoin", "bitcoin price", "btc price",
        "price of ethereum", "ethereum price", "eth price",
        "price of ", "crypto price", "cryptocurrency price",
        "how much is bitcoin", "how much is ethereum",
        "top 10 crypto", "top cryptocurrencies", "trending coins",
        "coin price", "token price", "solana price", "sol price",
        "bnb price", "doge price", "dogecoin price",
    ]
    NEWS_KEYWORDS = [
        "today's news", "latest news", "news today",
        "morning briefing", "daily briefing", "news briefing", "brief me",
        "what's happening", "tech news", "technology news", "business news",
        "sports news", "health news", "science news",
        "world news", "philippine news", "ph news",
        "news about ", "latest on ", "headlines", "top stories",
        "what's the news", "hacker news", "hn stories",
    ]
    FINANCE_KEYWORDS = [
        "convert ", " to php", " to usd", " to eur",
        "exchange rate", "currency rate", "currency conversion",
        "usd to php", "php to usd",
        "dollar to peso", "peso to dollar",
        "stock price", "share price", "nasdaq:",
        "apple stock", "tesla stock", "google stock", "amazon stock",
        "microsoft stock", "meta stock", "nvidia stock",
        "stock today", "stock market",
    ]
    REALTIME_KEYWORDS = [
        "what time is it", "what's the time", "current time",
        "what time in ", "time in ", "time now in",
        "what day is it", "today's date", "current date",
    ]
    FILE_FOLDER_KEYWORDS = [
        "list files in", "list files on", "show files in", "show files on",
        "what files are in", "what's in my ", "what is in my ",
        "open my documents", "open my downloads", "open my desktop",
        "open my pictures", "open my music", "open my videos",
        "open the documents folder", "open the downloads folder",
        "open the desktop folder", "open folder", "show folder",
        "find the file", "find file", "search for file",
        "locate the file", "where is the file",
        "open file explorer", "open explorer",
    ]
    # FIX 4: Broadened calculator keywords
    CALCULATOR_KEYWORDS = [
        "open calculator and", "open calc and",
        "calculate ", "compute ", "use calculator",
        "calculator: ", "calc: ",
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

    def _is_creative_chat(self, c):
        # FIX 1: Only creative if NOT paired with app-open command
        if any(k in c for k in self.APP_WRITE_KEYWORDS):
            return False
        return any(k in c for k in self.CREATIVE_CHAT_KEYWORDS)

    def _is_app_write(self, c):       return any(k in c for k in self.APP_WRITE_KEYWORDS)
    def _is_memory_task(self, c):     return any(k in c for k in self.MEMORY_KEYWORDS)
    def _is_screenshot_task(self, c): return any(k in c for k in self.SCREENSHOT_KEYWORDS)
    def _is_chrome_automation(self, c): return any(k in c for k in self.CHROME_AUTOMATION_KEYWORDS)
    def _is_pc_nav(self, c):          return any(k in c for k in self.PC_NAV_KEYWORDS)
    def _is_browser_task(self, c):    return any(k in c for k in self.BROWSER_KEYWORDS)
    def _is_research_task(self, c):   return any(k in c for k in self.RESEARCH_KEYWORDS)
    def _is_code_task(self, c):       return any(k in c for k in self.CODE_KEYWORDS)
    def _is_realtime_task(self, c):   return any(k in c for k in self.REALTIME_KEYWORDS)
    def _is_weather_task(self, c):    return any(k in c for k in self.WEATHER_KEYWORDS)
    def _is_crypto_task(self, c):     return any(k in c for k in self.CRYPTO_KEYWORDS)
    def _is_news_task(self, c):       return any(k in c for k in self.NEWS_KEYWORDS)
    def _is_finance_task(self, c):    return any(k in c for k in self.FINANCE_KEYWORDS)
    def _is_file_folder_task(self, c): return any(k in c for k in self.FILE_FOLDER_KEYWORDS)
    def _is_calculator_task(self, c): return any(k in c for k in self.CALCULATOR_KEYWORDS)

    def _is_open_app_command(self, c):
        OPEN_VERBS  = ["open ", "launch ", "start "]
        OFFICE_APPS = ["word","excel","powerpoint","ppt","notepad","notepad++",
                       "chrome","firefox","edge"]
        starts = any(c.startswith(v) or f" {v}" in c for v in OPEN_VERBS)
        app    = any(a in c for a in OFFICE_APPS)
        return (starts and app
                and not self._is_chrome_automation(c)
                and not self._is_pc_nav(c)
                and not self._is_file_folder_task(c))

    # =========================================================================
    # COMMAND CLASSIFIER
    # =========================================================================

    async def classify_command(self, command: str, user_context: str = "") -> str:
        c = command.lower()

        # Hard task routes — checked in strict priority order
        if self._is_memory_task(c):        return "task"
        if self._is_screenshot_task(c):    return "task"
        if self._is_app_write(c):          return "task"   # FIX 1: before creative chat
        if self._is_open_app_command(c):   return "task"   # FIX 1: before creative chat
        if self._is_chrome_automation(c):  return "task"
        if self._is_pc_nav(c):             return "task"
        if self._is_code_task(c):          return "task"
        if self._is_file_folder_task(c):   return "task"
        if self._is_calculator_task(c):    return "task"
        if self._is_weather_task(c):       return "task"
        if self._is_crypto_task(c):        return "task"
        if self._is_news_task(c):          return "task"
        if self._is_finance_task(c):       return "task"
        if self._is_browser_task(c):       return "task"
        if self._is_research_task(c):      return "task"
        if self._is_realtime_task(c):      return "task"
        if self._is_creative_chat(c):      return "chat"

        ctx_block = f"\n\nUser context:\n{user_context}" if user_context else ""
        system = (
            "Classify as 'chat' or 'task'.\n\n"
            "'chat': questions, explanations, creative writing, math, translations.\n"
            "'task': create files, control PC, write code programs/scripts, web browsing.\n"
            "Return ONLY: chat OR task"
        )
        result = await self.chat(system, command + ctx_block, temperature=0.0)
        return "chat" if "chat" in result.strip().lower() else "task"

    # =========================================================================
    # TASK PLANNER
    # =========================================================================

    async def plan_task(self, command: str, user_context: str = "") -> list[dict]:
        c = command.lower()

        if self._is_memory_task(c):
            return [{"step":1,"agent":"memory_agent",
                     "description":f"Memory: {command}",
                     "parameters":{"query":command}}]

        if self._is_screenshot_task(c):
            return [{"step":1,"agent":"automation_agent",
                     "description":"Take a screenshot",
                     "parameters":{"action":"screenshot_and_return"}}]

        # FIX 2: "open [app] and write X" — clean single sequence, no duplicate open
        if self._is_app_write(c):
            return self._build_app_write_plan(command, c)

        if self._is_chrome_automation(c):
            return self._build_chrome_automation_plan(command, c)

        if self._is_pc_nav(c):
            return self._build_pc_nav_plan(command, c)

        if self._is_file_folder_task(c):
            return self._build_file_folder_plan(command, c)

        if self._is_calculator_task(c):
            return self._build_calculator_plan(command, c)

        if self._is_weather_task(c):
            return [{"step":1,"agent":"weather_agent",
                     "description":f"Weather: {command}",
                     "parameters":{"query":command}}]

        if self._is_crypto_task(c):
            return [{"step":1,"agent":"crypto_agent",
                     "description":f"Crypto: {command}",
                     "parameters":{"query":command}}]

        if self._is_news_task(c):
            return [{"step":1,"agent":"news_agent",
                     "description":f"News: {command}",
                     "parameters":{"query":command}}]

        if self._is_finance_task(c):
            return [{"step":1,"agent":"finance_agent",
                     "description":f"Finance: {command}",
                     "parameters":{"query":command}}]

        if self._is_research_task(c):
            return [{"step":1,"agent":"research_agent",
                     "description":f"Research: {command}",
                     "parameters":{"query":command}}]

        if self._is_browser_task(c):
            return self._build_browser_plan(command, c)

        if self._is_realtime_task(c):
            return [{"step":1,"agent":"browser_agent",
                     "description":f"Real-time: {command}",
                     "parameters":{"action":"search","query":command,"engine":"google"}}]

        if self._is_code_task(c):
            lang = self._detect_language_hint(c)
            return [{"step":1,"agent":"coding_agent",
                     "description":f"Write code: {command}",
                     "parameters":{"task":command,"language":lang}}]

        # LLM planner for remaining
        is_open_app = self._is_open_app_command(c)
        no_doc_rule = (
            "\n⚠️ CRITICAL: 'open [app]' — use automation_agent ONLY, NEVER document_agent.\n"
            "NEVER generate both open_app AND new_file for the same app in the same plan.\n"
        ) if is_open_app else ""

        ctx_block = f"\n\nUser preferences:\n{user_context}" if user_context else ""

        system_prompt = (
            "You are AetherAI's task planner. Return ONLY a valid JSON array.\n\n"
            + no_doc_rule +
            "IMPORTANT: 'parameters' MUST be a JSON object {}, NEVER a string.\n"
            "IMPORTANT: Do NOT generate both open_app and new_file for the same app.\n\n"
            "automation_agent actions:\n"
            '  open_app:         {"action":"open_app","parameters":{"app":"notepad|word|excel|powerpoint|chrome|calc"}}\n'
            '  navigate_chrome:  {"action":"navigate_chrome","url":"https://..."}\n'
            '  calculator_input: {"action":"calculator_input","expression":"25*4"}\n'
            '  type:             {"action":"type","parameters":{"text":"__GENERATED_CONTENT__"}}\n'
            '  hotkey:           {"action":"hotkey","parameters":{"keys":["ctrl","s"]}}\n'
            '  wait:             {"action":"wait","parameters":{"ms":1000}}\n'
            '  list_files:       {"action":"list_files","path":"Documents"}\n'
            '  open_folder:      {"action":"open_folder","path":"Downloads"}\n\n'
            "Each step: {step, agent, description, parameters}"
        )

        raw = await self.chat(system_prompt, f'Plan: "{command}"{ctx_block}', temperature=0.2)
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

        try:
            plan = json.loads(raw)
            if not isinstance(plan, list): raise ValueError
            for step in plan:
                if isinstance(step.get("parameters"), str):
                    step["parameters"] = {"query": step["parameters"]}
            if is_open_app:
                plan = [s for s in plan if s.get("agent") != "document_agent"]
                plan = self._dedup_open_steps(plan)
                for idx, s in enumerate(plan, 1): s["step"] = idx
            return self._strip_trailing_steps(plan)
        except (json.JSONDecodeError, ValueError):
            return [{"step":1,"agent":"research_agent",
                     "description":f"Process: {command}",
                     "parameters":{"query":command}}]

    # =========================================================================
    # PLAN BUILDERS
    # =========================================================================

    def _build_app_write_plan(self, command: str, c: str) -> list[dict]:
        """
        FIX 2: 'open [app] and write/type X' → clean single-sequence plan.
        Uses new_file (not open_app) to avoid double-open, then types content.
        """
        # Determine which app
        if any(k in c for k in ["word", "winword"]):
            app = "word"
        elif "excel" in c:
            app = "excel"
        elif "powerpoint" in c or " ppt" in c:
            app = "powerpoint"
        elif "notepad++" in c:
            app = "notepad++"
        else:
            app = "notepad"

        return [
            {"step":1,"agent":"automation_agent",
             "description":f"Open {app}",
             "parameters":{"action":"new_file","parameters":{"app":app}}},
            {"step":2,"agent":"automation_agent",
             "description":"Wait for app to load",
             "parameters":{"action":"wait","parameters":{"ms":2000}}},
            {"step":3,"agent":"automation_agent",
             "description":"Type the requested content",
             "parameters":{"action":"type","parameters":{"text":"__GENERATED_CONTENT__"}}},
        ]

    def _build_chrome_automation_plan(self, command: str, c: str) -> list[dict]:
        """FIX 3: Reduced wait to 1000ms."""
        intent = c
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
             "parameters":{"action":"wait","parameters":{"ms":1000}}},   # FIX 3
            {"step":3,"agent":"automation_agent",
             "description":f"Navigate to: {url}",
             "parameters":{"action":"navigate_chrome","url":url}},
        ]

    def _build_pc_nav_plan(self, command: str, c: str) -> list[dict]:
        """FIX 3: Reduced wait to 1000ms."""
        url = "https://google.com"
        for site, site_url in self.SITE_URL_MAP.items():
            if site in c:
                url = site_url
                break
        return [
            {"step":1,"agent":"automation_agent",
             "description":"Open Chrome",
             "parameters":{"action":"open_app","parameters":{"app":"chrome"}}},
            {"step":2,"agent":"automation_agent",
             "description":"Wait for Chrome",
             "parameters":{"action":"wait","parameters":{"ms":1000}}},   # FIX 3
            {"step":3,"agent":"automation_agent",
             "description":f"Navigate to {url}",
             "parameters":{"action":"navigate_chrome","url":url}},
        ]

    def _build_calculator_plan(self, command: str, c: str) -> list[dict]:
        expression = self._extract_math_expression(command)
        return [
            {"step":1,"agent":"automation_agent",
             "description":"Open Calculator",
             "parameters":{"action":"open_app","parameters":{"app":"calculator"}}},
            {"step":2,"agent":"automation_agent",
             "description":"Wait for Calculator",
             "parameters":{"action":"wait","parameters":{"ms":1000}}},
            {"step":3,"agent":"automation_agent",
             "description":f"Calculate: {expression}",
             "parameters":{"action":"calculator_input","expression":expression}},
        ]

    def _build_file_folder_plan(self, command: str, c: str) -> list[dict]:
        if any(k in c for k in ["list files","show files","what files","what's in my","what is in my"]):
            folder = self._extract_folder_name(c)
            return [{"step":1,"agent":"automation_agent",
                     "description":f"List files in {folder}",
                     "parameters":{"action":"list_files","path":folder}}]
        if any(k in c for k in ["open my downloads","open the downloads","open downloads"]):
            return [{"step":1,"agent":"automation_agent","description":"Open Downloads",
                     "parameters":{"action":"open_folder","path":"Downloads"}}]
        if any(k in c for k in ["open my documents","open the documents","open documents"]):
            return [{"step":1,"agent":"automation_agent","description":"Open Documents",
                     "parameters":{"action":"open_folder","path":"Documents"}}]
        if any(k in c for k in ["open my desktop","open desktop"]):
            return [{"step":1,"agent":"automation_agent","description":"Open Desktop",
                     "parameters":{"action":"open_folder","path":"Desktop"}}]
        if any(k in c for k in ["open my pictures","open pictures"]):
            return [{"step":1,"agent":"automation_agent","description":"Open Pictures",
                     "parameters":{"action":"open_folder","path":"Pictures"}}]
        if any(k in c for k in ["open my music","open music"]):
            return [{"step":1,"agent":"automation_agent","description":"Open Music",
                     "parameters":{"action":"open_folder","path":"Music"}}]
        if any(k in c for k in ["open my videos","open videos"]):
            return [{"step":1,"agent":"automation_agent","description":"Open Videos",
                     "parameters":{"action":"open_folder","path":"Videos"}}]
        if "open folder" in c or "show folder" in c:
            path = re.sub(r".*(open|show)\s+folder\s*","",c,flags=re.IGNORECASE).strip()
            return [{"step":1,"agent":"automation_agent","description":f"Open folder: {path}",
                     "parameters":{"action":"open_folder","path":path or "home"}}]
        if any(k in c for k in ["find the file","find file","locate the file","search for file"]):
            filename = self._extract_filename(command)
            return [{"step":1,"agent":"automation_agent","description":f"Find: {filename}",
                     "parameters":{"action":"find_and_open_file","filename":filename,
                                   "search_in":str(__import__('pathlib').Path.home())}}]
        if any(k in c for k in ["open file explorer","open explorer"]):
            return [{"step":1,"agent":"automation_agent","description":"Open File Explorer",
                     "parameters":{"action":"open_app","parameters":{"app":"explorer"}}}]
        folder = self._extract_folder_name(c)
        return [{"step":1,"agent":"automation_agent","description":f"Open folder: {folder}",
                 "parameters":{"action":"open_folder","path":folder}}]

    def _build_browser_plan(self, command: str, c: str) -> list[dict]:
        if any(k in c for k in ["search youtube for","on youtube","find on youtube",
                                  "youtube search","youtube video"]):
            query = c
            for pat in ["search youtube for","on youtube","find on youtube","youtube search"]:
                query = re.sub(rf".*{re.escape(pat)}\s*","",query,flags=re.IGNORECASE).strip()
            return [{"step":1,"agent":"browser_agent",
                     "description":f"Search YouTube: {query or command}",
                     "parameters":{"action":"youtube","query":query or command}}]
        if any(k in c for k in ["hacker news","hackernews","ycombinator"]):
            return [{"step":1,"agent":"browser_agent","description":"Hacker News",
                     "parameters":{"action":"workflow","goal":command}}]
        if "wikipedia" in c or "wiki/" in c:
            url_m = re.search(r"(https?://[^\s]+|wikipedia\.org/wiki/[^\s]+)",c)
            if url_m:
                url = url_m.group(1)
                if not url.startswith("http"): url = "https://" + url
                return [{"step":1,"agent":"browser_agent","description":"Scrape Wikipedia",
                         "parameters":{"action":"scrape","url":url}}]
            return [{"step":1,"agent":"browser_agent","description":"Wikipedia search",
                     "parameters":{"action":"search","query":command,"engine":"google"}}]
        url_m = re.search(r"https?://[^\s]+",command)
        if url_m:
            return [{"step":1,"agent":"browser_agent","description":"Scrape URL",
                     "parameters":{"action":"scrape","url":url_m.group(0)}}]
        dest_m = re.search(
            r"(?:go to|open|visit|at|summarize.*?at)\s+([\w\-\.]+\.[\w]{2,}[^\s]*)",c)
        if dest_m:
            url = dest_m.group(1)
            if not url.startswith("http"): url = "https://" + url
            return [{"step":1,"agent":"browser_agent","description":"Scrape",
                     "parameters":{"action":"scrape","url":url}}]
        if any(k in c for k in ["search google for","google for","google search",
                                  "search the web for","search bing for",
                                  "search the internet for"]):
            query = re.sub(
                r".*(search google for|google for|search the web for|"
                r"search bing for|google search for?|search the internet for)\s*",
                "",c,flags=re.IGNORECASE).strip() or command
            engine = "bing" if "bing" in c else "google"
            return [{"step":1,"agent":"browser_agent","description":f"Web search: {query}",
                     "parameters":{"action":"search","query":query,"engine":engine}}]
        return [{"step":1,"agent":"browser_agent","description":f"Web search: {command}",
                 "parameters":{"action":"search","query":command,"engine":"google"}}]

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _dedup_open_steps(self, plan: list) -> list:
        """FIX 2: Remove duplicate open_app/new_file steps for the same app."""
        seen_apps = set()
        result = []
        for step in plan:
            params = step.get("parameters", {})
            action = params.get("action", "")
            if action in ("open_app", "new_file"):
                inner = params.get("parameters", {})
                app = inner.get("app", "").lower() if isinstance(inner, dict) else ""
                if app in seen_apps:
                    continue  # skip duplicate
                if app:
                    seen_apps.add(app)
            result.append(step)
        return result

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

    def _extract_math_expression(self, command: str) -> str:
        # Look for math expression with digits and operators
        m = re.search(r"([\d\s\+\-\*\/\(\)\.%]+)", command)
        if m:
            expr = m.group(1).strip()
            if any(c.isdigit() for c in expr) and len(expr) > 1:
                return expr
        clean = re.sub(
            r"\b(calculate|compute|solve|open calculator and|use calculator|"
            r"what is the result of|open calc and|calculator)\b",
            "", command, flags=re.IGNORECASE
        ).strip()
        return clean or "0"

    def _extract_folder_name(self, c: str) -> str:
        known = {
            "documents":"Documents","downloads":"Downloads",
            "desktop":"Desktop","pictures":"Pictures",
            "music":"Music","videos":"Videos","home":"home",
        }
        for key, folder in known.items():
            if key in c: return folder
        m = re.search(r"(?:in|on|from)\s+(?:my\s+)?(.+?)(?:\s+folder)?$", c)
        if m:
            name = m.group(1).strip()
            return known.get(name.lower(), name.title())
        return "home"

    def _extract_filename(self, command: str) -> str:
        patterns = [
            r"(?:called|named|file)\s+(\S+)",
            r"find\s+(?:the\s+file\s+)?(\S+\.\w+)",
        ]
        for pat in patterns:
            m = re.search(pat, command, re.IGNORECASE)
            if m:
                return m.group(1).strip("\"'")
        return command.split()[-1]

    def _detect_language_hint(self, c: str) -> str:
        if "python" in c:     return "python"
        if "javascript" in c: return "javascript"
        if "typescript" in c: return "typescript"
        if "c++" in c:        return "cpp"
        if " java " in c:     return "java"
        if "rust" in c:       return "rust"
        if "golang" in c:     return "go"
        if " c " in c and "program" in c: return "c"
        return ""

    def _strip_trailing_steps(self, plan: list) -> list:
        TERMINAL = {"document_agent","coding_agent"}
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
            r"\b(open|launch|use)\s+(notepad\+\+|notepad|word|excel|powerpoint|a file)\s*(and|then|to)?\s*",
            "", command, flags=re.IGNORECASE
        ).strip() or command
        return await self.chat(system, f"Write: {clean}", temperature=0.7)
