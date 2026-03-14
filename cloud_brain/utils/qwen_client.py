"""
AetherAI — Qwen API Client  (Stage 6)

Stage 6 additions to routing:
  • weather_agent  — weather/forecast/temperature/rain queries
  • crypto_agent   — cryptocurrency price queries
  • news_agent     — news/briefing/headlines queries
  • finance_agent  — currency conversion, exchange rates, stock prices

All Stage 5 patch fixes retained:
  • Creative writing → chat
  • Research keyword → research_agent
  • Browser: specific URLs, YouTube, web search
  • PC automation: "open chrome and X", "go to [site]"
  • Rich answer() prompts
  • source="web" on summarize()
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
            "- For factual questions: provide complete explanations with context.\n"
            "- For creative writing (poems, haikus, stories, jokes): produce the "
            "full creative piece immediately, then optionally add brief context.\n"
            "- For math: show the working, then give the answer.\n"
            "- For translations: give the translation and explain pronunciation.\n"
            "- For health/science: give comprehensive response with specific details.\n"
            "- Use markdown formatting for structured topics.\n"
            "- Never say 'I cannot access real-time data' — answer from knowledge."
        )
        parts = []
        if user_context: parts.append(f"Facts about this user:\n{user_context}")
        if context:      parts.append(f"Additional context:\n{context}")
        parts.append(f"Request: {question}")
        return await self.chat(system, "\n\n".join(parts), temperature=0.7)

    async def summarize(self, content: str, context: str = "",
                        source: str = "") -> str:
        if source == "web":
            system = (
                "You are an information analyst. Content was JUST fetched live from the web.\n"
                "CRITICAL RULES:\n"
                "- NEVER say 'as of my knowledge cutoff' or 'I cannot access real-time data'.\n"
                "- Base your summary ENTIRELY on the provided content.\n"
                "- Be thorough — include all key facts, numbers, names, and specific details.\n"
                "- Use clear structure with headings and bullet points where appropriate."
            )
        else:
            system = (
                "You are a thorough content analyst. "
                "Summarize in detail — include all key facts, figures, and important details. "
                "Use clear structure. Do not truncate or oversimplify."
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

    CREATIVE_CHAT_KEYWORDS = [
        "write me a haiku", "write a haiku",
        "write me a poem", "write a poem", "compose a poem",
        "write me a song", "write a song",
        "write me a story", "write a short story", "tell me a story",
        "write me a joke", "tell me a joke", "tell me a riddle",
        "write me a limerick", "write a limerick",
        "write me a sonnet", "write a sonnet",
        "write a letter to ", "write me a letter",
        "write an essay about", "write me an essay",
        "write a paragraph about",
        "write me a rap", "write a rap about",
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
        "look up on google",
        "wikipedia", "wiki/",
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

    # ── Stage 6: New agent keyword sets ──────────────────────────────────────

    WEATHER_KEYWORDS = [
        "weather in ", "weather for ", "weather today", "weather tomorrow",
        "what's the weather", "what is the weather",
        "temperature in ", "temperature today",
        "will it rain", "is it raining", "forecast for ", "forecast in ",
        "how hot is ", "how cold is ", "humidity in ",
        "is it sunny", "is it cloudy", "is it snowing",
        "climate in ",
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
        "what's happening", "what is happening",
        "tech news", "technology news", "business news",
        "sports news", "health news", "science news",
        "world news", "philippine news", "ph news",
        "news about ", "latest on ", "headlines",
        "top stories", "what's the news",
        "hacker news", "hn stories",
    ]

    FINANCE_KEYWORDS = [
        "convert ", " to php", " to usd", " to eur",
        "exchange rate", "currency rate", "currency conversion",
        "how much is ", "usd to php", "php to usd",
        "dollar to peso", "peso to dollar",
        "stock price", "share price", "nasdaq:", "nyse:",
        "apple stock", "tesla stock", "google stock", "amazon stock",
        "microsoft stock", "meta stock", "nvidia stock",
        "stock today", "stock market",
    ]

    REALTIME_KEYWORDS = [
        "what time is it", "what's the time", "current time",
        "what time in ", "time in ", "time now in",
        "what day is it", "today's date", "current date",
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

    def _is_creative_chat(self, c: str) -> bool:
        return any(k in c for k in self.CREATIVE_CHAT_KEYWORDS)
    def _is_memory_task(self, c: str) -> bool:
        return any(k in c for k in self.MEMORY_KEYWORDS)
    def _is_screenshot_task(self, c: str) -> bool:
        return any(k in c for k in self.SCREENSHOT_KEYWORDS)
    def _is_chrome_automation(self, c: str) -> bool:
        return any(k in c for k in self.CHROME_AUTOMATION_KEYWORDS)
    def _is_pc_nav(self, c: str) -> bool:
        return any(k in c for k in self.PC_NAV_KEYWORDS)
    def _is_browser_task(self, c: str) -> bool:
        return any(k in c for k in self.BROWSER_KEYWORDS)
    def _is_research_task(self, c: str) -> bool:
        return any(k in c for k in self.RESEARCH_KEYWORDS)
    def _is_code_task(self, c: str) -> bool:
        return any(k in c for k in self.CODE_KEYWORDS)
    def _is_realtime_task(self, c: str) -> bool:
        return any(k in c for k in self.REALTIME_KEYWORDS)
    def _is_weather_task(self, c: str) -> bool:
        return any(k in c for k in self.WEATHER_KEYWORDS)
    def _is_crypto_task(self, c: str) -> bool:
        return any(k in c for k in self.CRYPTO_KEYWORDS)
    def _is_news_task(self, c: str) -> bool:
        return any(k in c for k in self.NEWS_KEYWORDS)
    def _is_finance_task(self, c: str) -> bool:
        return any(k in c for k in self.FINANCE_KEYWORDS)

    def _is_open_app_command(self, c: str) -> bool:
        OPEN_VERBS  = ["open ", "launch ", "start "]
        OFFICE_APPS = ["word","excel","powerpoint","ppt","notepad","notepad++",
                       "chrome","firefox","edge"]
        starts = any(c.startswith(v) or f" {v}" in c for v in OPEN_VERBS)
        app    = any(a in c for a in OFFICE_APPS)
        return (starts and app
                and not self._is_chrome_automation(c)
                and not self._is_pc_nav(c))

    # =========================================================================
    # COMMAND CLASSIFIER
    # =========================================================================

    async def classify_command(self, command: str, user_context: str = "") -> str:
        c = command.lower()

        # Hard task routes — checked in priority order
        if self._is_memory_task(c):       return "task"
        if self._is_screenshot_task(c):   return "task"
        if self._is_chrome_automation(c): return "task"
        if self._is_pc_nav(c):            return "task"
        if self._is_code_task(c):         return "task"
        # Stage 6 new agents
        if self._is_weather_task(c):      return "task"
        if self._is_crypto_task(c):       return "task"
        if self._is_news_task(c):         return "task"
        if self._is_finance_task(c):      return "task"
        # Existing agents
        if self._is_browser_task(c):      return "task"
        if self._is_research_task(c):     return "task"
        if self._is_realtime_task(c):     return "task"

        # Creative writing → always chat
        if self._is_creative_chat(c):     return "chat"

        ctx_block = f"\n\nUser context:\n{user_context}" if user_context else ""
        system = (
            "Classify as 'chat' or 'task'.\n\n"
            "'chat': questions, explanations, creative writing, math, translations, "
            "general knowledge, advice.\n"
            "'task': create files, control PC, write code programs/scripts, "
            "web browsing, scraping, multi-step workflows.\n\n"
            "Creative text (poems, stories, haikus) = chat. "
            "Code programs/scripts = task.\n\n"
            "Return ONLY: chat OR task"
        )
        result = await self.chat(system, command + ctx_block, temperature=0.0)
        return "chat" if "chat" in result.strip().lower() else "task"

    # =========================================================================
    # TASK PLANNER
    # =========================================================================

    async def plan_task(self, command: str, user_context: str = "") -> list[dict]:
        c = command.lower()

        # Hard-routes in strict priority order
        if self._is_memory_task(c):
            return [{"step":1,"agent":"memory_agent",
                     "description":f"Memory: {command}",
                     "parameters":{"query":command}}]

        if self._is_screenshot_task(c):
            return [{"step":1,"agent":"automation_agent",
                     "description":"Take a screenshot",
                     "parameters":{"action":"screenshot_and_return"}}]

        if self._is_chrome_automation(c):
            return self._build_chrome_automation_plan(command, c)

        if self._is_pc_nav(c):
            return self._build_pc_nav_plan(command, c)

        # Stage 6: new agents take priority over browser for their domains
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

        # LLM planner for everything else
        is_open_app = self._is_open_app_command(c)
        no_doc_rule = (
            "\n⚠️ CRITICAL: 'open [app]' — use automation_agent ONLY, NEVER document_agent.\n"
        ) if is_open_app else ""

        ctx_block = f"\n\nUser preferences:\n{user_context}" if user_context else ""

        system_prompt = (
            "You are AetherAI's task planner. Return ONLY a valid JSON array.\n\n"
            + no_doc_rule +
            "AGENTS:\n"
            "weather_agent   — weather, forecasts, temperature for any city\n"
            '  {"query":"what is the weather in Manila"}\n\n'
            "crypto_agent    — cryptocurrency prices, market data\n"
            '  {"query":"bitcoin price"}\n\n'
            "news_agent      — news headlines, briefings\n"
            '  {"query":"tech news today"}\n\n'
            "finance_agent   — currency conversion, exchange rates, stocks\n"
            '  {"query":"convert 500 USD to PHP"}\n\n'
            "research_agent  — academic research with citations\n"
            '  {"query":"..."}\n\n'
            "browser_agent   — scrape URLs, YouTube, web search\n"
            '  {"action":"scrape","url":"https://..."}\n'
            '  {"action":"youtube","query":"..."}\n'
            '  {"action":"search","query":"...","engine":"google"}\n\n'
            "document_agent  — create .pptx/.docx/.xlsx  [TERMINAL]\n"
            '  {"type":"presentation"|"document"|"spreadsheet","topic":"..."}\n\n'
            "coding_agent    — write and save code files  [TERMINAL]\n"
            '  {"task":"...","language":"python|js|..."}\n\n'
            "automation_agent — control the physical PC\n"
            '  {"action":"open_app","parameters":{"app":"..."}}\n'
            '  {"action":"type","parameters":{"text":"__GENERATED_CONTENT__"}}\n\n'
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

    def _build_browser_plan(self, command: str, c: str) -> list[dict]:
        if any(k in c for k in ["search youtube for","on youtube","find on youtube",
                                  "youtube search","youtube video"]):
            query = c
            for pat in ["search youtube for","on youtube","find on youtube","youtube search"]:
                query = re.sub(rf".*{re.escape(pat)}\s*", "", query,
                               flags=re.IGNORECASE).strip()
            return [{"step":1,"agent":"browser_agent",
                     "description":f"Search YouTube: {query or command}",
                     "parameters":{"action":"youtube","query":query or command}}]

        if any(k in c for k in ["hacker news","hackernews","ycombinator"]):
            return [{"step":1,"agent":"browser_agent",
                     "description":"Hacker News",
                     "parameters":{"action":"workflow","goal":command}}]

        if "wikipedia" in c or "wiki/" in c:
            url_m = re.search(r"(https?://[^\s]+|wikipedia\.org/wiki/[^\s]+)", c)
            if url_m:
                url = url_m.group(1)
                if not url.startswith("http"): url = "https://" + url
                return [{"step":1,"agent":"browser_agent",
                         "description":f"Scrape Wikipedia",
                         "parameters":{"action":"scrape","url":url}}]
            return [{"step":1,"agent":"browser_agent",
                     "description":f"Wikipedia search",
                     "parameters":{"action":"search","query":command,"engine":"google"}}]

        url_m = re.search(r"https?://[^\s]+", command)
        if url_m:
            return [{"step":1,"agent":"browser_agent",
                     "description":f"Scrape URL",
                     "parameters":{"action":"scrape","url":url_m.group(0)}}]

        dest_m = re.search(
            r"(?:go to|open|visit|at|summarize.*?at)\s+([\w\-\.]+\.[\w]{2,}[^\s]*)", c)
        if dest_m:
            url = dest_m.group(1)
            if not url.startswith("http"): url = "https://" + url
            return [{"step":1,"agent":"browser_agent",
                     "description":f"Scrape",
                     "parameters":{"action":"scrape","url":url}}]

        if any(k in c for k in ["search google for","google for","google search",
                                  "search the web for","search bing for",
                                  "search the internet for"]):
            query = re.sub(
                r".*(search google for|google for|search the web for|"
                r"search bing for|google search for?|search the internet for)\s*",
                "", c, flags=re.IGNORECASE
            ).strip() or command
            engine = "bing" if "bing" in c else "google"
            return [{"step":1,"agent":"browser_agent",
                     "description":f"Web search",
                     "parameters":{"action":"search","query":query,"engine":engine}}]

        return [{"step":1,"agent":"browser_agent",
                 "description":f"Web search: {command}",
                 "parameters":{"action":"search","query":command,"engine":"google"}}]

    # =========================================================================
    # PC AUTOMATION PLAN BUILDERS
    # =========================================================================

    def _build_chrome_automation_plan(self, command: str, c: str) -> list[dict]:
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
             "parameters":{"action":"wait","parameters":{"ms":2500}}},
            {"step":3,"agent":"automation_agent",
             "description":f"Navigate to: {url}",
             "parameters":{"action":"type","parameters":{"text":url}}},
            {"step":4,"agent":"automation_agent",
             "description":"Go",
             "parameters":{"action":"hotkey","parameters":{"keys":["enter"]}}},
        ]

    def _build_pc_nav_plan(self, command: str, c: str) -> list[dict]:
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
