"""
AetherAI — Qwen API Client  (Stage 5 — patch 2)

Fixes applied this patch
────────────────────────
FIX P3  "open chrome and go to X" / "open chrome and search X" were routing
        to browser_agent (headless Playwright). Users with a connected device
        expect their PHYSICAL Chrome to open on the PC. These commands now
        route to automation_agent and produce a proper sequence:
          1. open_app chrome
          2. wait 2500ms for Chrome to load
          3. type the URL / search query
          4. hotkey Enter

        Removed CHROME_WITH_ACTION_KEYWORDS from the browser routing path
        entirely. "open chrome and [action]" is now treated as an automation
        task, not a browser task.

FIX P4  Plain navigation commands like "go to youtube", "open youtube",
        "open facebook" with a device connected now produce automation
        sequences that open Chrome on the PC and navigate there, rather than
        running a headless browser scrape.

        The routing priority is:
          memory task   → memory_agent   (hard-route, always)
          screenshot    → automation_agent (hard-route, always)
          open app + action → automation_agent PC sequence
          youtube search    → browser_agent youtube action
          explicit URL/site → browser_agent scrape/workflow
          general web   → browser_agent search
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
        user_content = [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}},
            {"type": "text",      "text": user_message},
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
            logger.warning(
                f"[QwenClient] chat_with_image failed ({e}). "
                "Falling back to text-only. Set QWEN_VISION_MODEL=qwen-vl-plus in env."
            )
            return await self.chat(system_prompt, user_message, temperature)

    # ── Keyword sets ──────────────────────────────────────────────────────────

    # FIX P3: CHROME_WITH_ACTION_KEYWORDS removed from browser routing.
    # "open chrome and X" is now an automation sequence, not a browser task.

    BROWSER_KEYWORDS = [
        "using browser agent", "use browser agent", "browser agent",
        "search youtube", "on youtube", "youtube for", "find on youtube",
        "youtube search", "youtube video", "youtube channel",
        "wikipedia", "wiki/",
        "go to http", "go to www",
        "visit http", "navigate to http",
        "search google for", "google for", "google search",
        "search the web for", "search bing",
        "scrape", "extract from", "read the article at",
        "summarize the page", "summarize the website",
        "summarize the article at",
    ]

    # "open chrome and [action]" → automation_agent PC sequence
    CHROME_AUTOMATION_KEYWORDS = [
        "open chrome and", "launch chrome and", "open browser and",
        "open firefox and", "open chrome to", "open chrome then",
        "open edge and",
    ]

    # Direct PC navigation: open/go to a site by name with device present
    # These build an automation sequence: open_app chrome → type URL → Enter
    PC_NAV_KEYWORDS = [
        "open youtube", "go to youtube", "open facebook", "go to facebook",
        "open reddit", "go to reddit", "open twitter", "go to twitter",
        "open instagram", "go to instagram", "open github", "go to github",
        "open netflix", "go to netflix", "open spotify", "go to spotify",
        "open linkedin", "go to linkedin", "open tiktok", "go to tiktok",
        "open amazon", "go to amazon", "open gmail", "go to gmail",
        "open stackoverflow", "go to stackoverflow",
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
        "take a screen capture", "screen capture", "take a screenshoot",
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

    # Map site name → URL for PC navigation sequences
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

    def _is_browser_task(self, cmd_lc: str) -> bool:
        return any(k in cmd_lc for k in self.BROWSER_KEYWORDS)

    def _is_chrome_automation(self, cmd_lc: str) -> bool:
        """FIX P3: 'open chrome and X' → PC automation sequence."""
        return any(k in cmd_lc for k in self.CHROME_AUTOMATION_KEYWORDS)

    def _is_pc_nav(self, cmd_lc: str) -> bool:
        """FIX P4: 'open/go to [site name]' → PC automation sequence."""
        return any(k in cmd_lc for k in self.PC_NAV_KEYWORDS)

    def _is_memory_task(self, cmd_lc: str) -> bool:
        return any(k in cmd_lc for k in self.MEMORY_KEYWORDS)

    def _is_open_app_command(self, cmd_lc: str) -> bool:
        OPEN_VERBS  = ["open ", "launch ", "start "]
        OFFICE_APPS = ["word", "excel", "powerpoint", "ppt", "notepad", "notepad++",
                       "chrome", "firefox", "edge"]
        starts_with_open   = any(cmd_lc.startswith(v) or f" {v}" in cmd_lc for v in OPEN_VERBS)
        mentions_app       = any(app in cmd_lc for app in OFFICE_APPS)
        has_chrome_action  = self._is_chrome_automation(cmd_lc)
        has_pc_nav         = self._is_pc_nav(cmd_lc)
        return starts_with_open and mentions_app and not has_chrome_action and not has_pc_nav

    def _is_screenshot_task(self, cmd_lc: str) -> bool:
        return any(k in cmd_lc for k in self.SCREENSHOT_KEYWORDS)

    def _is_realtime_task(self, cmd_lc: str) -> bool:
        return any(k in cmd_lc for k in self.REALTIME_KEYWORDS)

    # ── Command classifier ────────────────────────────────────────────────────

    async def classify_command(self, command: str, user_context: str = "") -> str:
        cmd_lc = command.lower()

        if self._is_memory_task(cmd_lc):           return "task"
        if any(k in cmd_lc for k in self.CODE_KEYWORDS): return "task"
        if self._is_screenshot_task(cmd_lc):        return "task"
        if self._is_realtime_task(cmd_lc):          return "task"
        if self._is_chrome_automation(cmd_lc):      return "task"
        if self._is_pc_nav(cmd_lc):                 return "task"
        if self._is_browser_task(cmd_lc):           return "task"

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
        cmd_lc = command.lower()

        # Hard-routes (checked before Qwen)
        if self._is_memory_task(cmd_lc):
            return [{"step":1,"agent":"memory_agent",
                     "description":f"Memory operation: {command}",
                     "parameters":{"query":command}}]

        if self._is_screenshot_task(cmd_lc):
            return [{"step":1,"agent":"automation_agent",
                     "description":"Take a screenshot of the screen",
                     "parameters":{"action":"screenshot_and_return"}}]

        if self._is_realtime_task(cmd_lc) and not self._is_browser_task(cmd_lc):
            return [{"step":1,"agent":"browser_agent",
                     "description":f"Search for: {command}",
                     "parameters":{"action":"search","query":command,"engine":"google"}}]

        # FIX P3: "open chrome and go to / search for X" → automation sequence on PC
        if self._is_chrome_automation(cmd_lc):
            return self._build_chrome_automation_plan(command, cmd_lc)

        # FIX P4: "go to youtube / open facebook" etc → automation sequence on PC
        if self._is_pc_nav(cmd_lc):
            return self._build_pc_nav_plan(command, cmd_lc)

        if self._is_browser_task(cmd_lc):
            return self._build_browser_plan(command, cmd_lc)

        # ── Qwen planner for everything else ─────────────────────────────────
        is_open_app = self._is_open_app_command(cmd_lc)

        no_doc_agent_rule = ""
        if is_open_app:
            no_doc_agent_rule = (
                "\n⚠️ CRITICAL OVERRIDE: This command says 'open [app]'. "
                "You MUST use automation_agent. You MUST NOT use document_agent.\n"
            )

        ctx_block = f"\n\nUser preferences:\n{user_context}" if user_context else ""

        system_prompt = (
            "You are AetherAI's task planner built by Patrick Perez.\n"
            "Return ONLY a valid JSON array of steps. No explanation. No markdown fences.\n\n"
            + no_doc_agent_rule +
            "Each step: {step, agent, description, parameters}\n\n"

            "AGENTS:\n\n"
            "research_agent  — DuckDuckGo search + summarization\n"
            '  {"query":"..."}\n\n'
            "browser_agent   — headless Chromium (Playwright)\n"
            '  {"action":"search","query":"...","engine":"google"}\n'
            '  {"action":"youtube","query":"..."}\n'
            '  {"action":"scrape","url":"https://..."}\n'
            '  {"action":"workflow","goal":"...","url":"https://..."}\n\n'
            "document_agent  — create .pptx/.docx/.xlsx files\n"
            '  {"type":"presentation"|"document"|"spreadsheet","topic":"..."}\n'
            "  TERMINAL step — no research steps after it.\n\n"
            "coding_agent    — write and save code\n"
            '  {"task":"...","language":"python|js|..."}\n'
            "  TERMINAL step.\n\n"
            "automation_agent — control the physical PC\n"
            '  open_app:   {"action":"open_app","parameters":{"app":"chrome|notepad|word|..."}}\n'
            '  new_file:   {"action":"new_file","parameters":{"app":"notepad|word|excel|..."}}\n'
            '  type:       {"action":"type","parameters":{"text":"__GENERATED_CONTENT__"}}\n'
            '  hotkey:     {"action":"hotkey","parameters":{"keys":["ctrl","s"]}}\n'
            '  wait:       {"action":"wait","parameters":{"ms":2000}}\n'
            '  screenshot: {"action":"screenshot_and_return"}\n\n'
            "memory_agent    — save/recall/forget user preferences\n"
            '  {"query":"full user statement"}\n\n'

            "ROUTING RULES:\n"
            "1. 'open chrome and go to X' or 'go to [site name]' → automation_agent sequence\n"
            "2. 'open word/excel/ppt and write X' → automation_agent only\n"
            "3. 'create presentation/doc/spreadsheet' → document_agent (single step)\n"
            "4. 'write python/C code' → coding_agent (single step)\n"
            "5. general research → research_agent\n"
            "6. NEVER write long content inline — use __GENERATED_CONTENT__\n"
            "7. memory keywords → memory_agent\n"
            "8. screenshot → automation_agent screenshot_and_return\n"
        )

        raw = await self.chat(system_prompt, f'Plan: "{command}"{ctx_block}', temperature=0.2)
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
            return [{"step":1,"agent":"research_agent",
                     "description":f"Process: {command}",
                     "parameters":{"query":command}}]

    # ── PC automation plan builders ───────────────────────────────────────────

    def _build_chrome_automation_plan(self, command: str, cmd_lc: str) -> list[dict]:
        """
        FIX P3: Build an automation sequence for 'open chrome and [action]'.
        Determines whether the action is navigation or a search query.
        """
        # Strip the "open chrome and" prefix to get the intent
        intent = cmd_lc
        for prefix in self.CHROME_AUTOMATION_KEYWORDS:
            intent = re.sub(rf".*{re.escape(prefix)}\s*", "", intent, flags=re.IGNORECASE).strip()

        # Determine URL vs search query
        url = self._extract_url_from_intent(intent)

        return [
            {"step":1,"agent":"automation_agent",
             "description":"Open Google Chrome on the PC",
             "parameters":{"action":"open_app","parameters":{"app":"chrome"}}},
            {"step":2,"agent":"automation_agent",
             "description":"Wait for Chrome to load",
             "parameters":{"action":"wait","parameters":{"ms":2500}}},
            {"step":3,"agent":"automation_agent",
             "description":f"Navigate to: {url}",
             "parameters":{"action":"type","parameters":{"text":url}}},
            {"step":4,"agent":"automation_agent",
             "description":"Press Enter to go",
             "parameters":{"action":"hotkey","parameters":{"keys":["enter"]}}},
        ]

    def _build_pc_nav_plan(self, command: str, cmd_lc: str) -> list[dict]:
        """
        FIX P4: Build an automation sequence for 'go to [site]' / 'open [site]'.
        Opens Chrome and navigates to the site URL.
        """
        # Find which site was mentioned
        url = "https://google.com"
        for site, site_url in self.SITE_URL_MAP.items():
            if site in cmd_lc:
                url = site_url
                break

        return [
            {"step":1,"agent":"automation_agent",
             "description":"Open Google Chrome on the PC",
             "parameters":{"action":"open_app","parameters":{"app":"chrome"}}},
            {"step":2,"agent":"automation_agent",
             "description":"Wait for Chrome to load",
             "parameters":{"action":"wait","parameters":{"ms":2500}}},
            {"step":3,"agent":"automation_agent",
             "description":f"Navigate to {url}",
             "parameters":{"action":"type","parameters":{"text":url}}},
            {"step":4,"agent":"automation_agent",
             "description":"Press Enter to go",
             "parameters":{"action":"hotkey","parameters":{"keys":["enter"]}}},
        ]

    def _extract_url_from_intent(self, intent: str) -> str:
        """
        Given the part after 'open chrome and', determine the best URL to type.
        Returns a full URL or a search query string that Chrome will search.
        """
        # Explicit URL in command
        url_match = re.search(r"https?://\S+", intent)
        if url_match:
            return url_match.group(0)

        # Known site name
        for site, url in self.SITE_URL_MAP.items():
            patterns = [
                rf"go to {site}", rf"navigate to {site}", rf"open {site}",
                rf"visit {site}", rf"to {site}\.com", rf"^{site}$",
            ]
            for pat in patterns:
                if re.search(pat, intent, re.IGNORECASE):
                    return url

        # "search for X" or "search X" → let Chrome search via omnibox
        search_match = re.search(
            r"(?:search(?: for)?|look up|find)\s+(.+)", intent, re.IGNORECASE
        )
        if search_match:
            return search_match.group(1).strip()

        # Bare domain like "youtube.com"
        domain_match = re.search(r"[\w\-]+\.(com|org|net|io|co|app|dev)\b", intent)
        if domain_match:
            return "https://" + domain_match.group(0)

        # Fallback: treat the whole intent as a Chrome omnibox query
        return intent or "https://google.com"

    # ── Browser plan builder ──────────────────────────────────────────────────

    def _build_browser_plan(self, command: str, cmd_lc: str) -> list[dict]:
        SITE_MAP = {
            "youtube": "https://youtube.com", "facebook": "https://facebook.com",
            "twitter": "https://twitter.com", "instagram": "https://instagram.com",
            "reddit":  "https://reddit.com",  "github":   "https://github.com",
            "stackoverflow": "https://stackoverflow.com",
            "netflix": "https://netflix.com", "amazon":   "https://amazon.com",
            "gmail":   "https://gmail.com",   "linkedin": "https://linkedin.com",
        }

        if "youtube" in cmd_lc:
            YOUTUBE_NAV    = ["go to youtube", "open youtube", "navigate to youtube",
                              "visit youtube"]
            YOUTUBE_SEARCH = ["search youtube for", "youtube for", "on youtube",
                               "find on youtube", "youtube search", "search on youtube"]
            has_nav    = any(k in cmd_lc for k in YOUTUBE_NAV)
            has_search = any(k in cmd_lc for k in YOUTUBE_SEARCH)
            if has_nav and not has_search:
                return [{"step":1,"agent":"browser_agent",
                         "description":"Navigate to YouTube",
                         "parameters":{"action":"workflow","goal":"open youtube",
                                       "url":"https://youtube.com"}}]
            query = cmd_lc
            for pat in YOUTUBE_SEARCH + ["search youtube"]:
                query = re.sub(rf".*{re.escape(pat)}\s*", "", query, flags=re.IGNORECASE).strip()
            if not query or len(query) < 2:
                return [{"step":1,"agent":"browser_agent",
                         "description":"Navigate to YouTube",
                         "parameters":{"action":"workflow","goal":"open youtube",
                                       "url":"https://youtube.com"}}]
            return [{"step":1,"agent":"browser_agent",
                     "description":f"Search YouTube for: {query}",
                     "parameters":{"action":"youtube","query":query}}]

        if "wikipedia" in cmd_lc or "wiki/" in cmd_lc:
            url_match = re.search(r"(https?://[^\s]+|wikipedia\.org/wiki/[^\s]+)", cmd_lc)
            if url_match:
                url = url_match.group(1)
                if not url.startswith("http"): url = "https://" + url
                return [{"step":1,"agent":"browser_agent",
                         "description":f"Read Wikipedia: {url}",
                         "parameters":{"action":"scrape","url":url}}]
            return [{"step":1,"agent":"browser_agent",
                     "description":f"Search Wikipedia: {command}",
                     "parameters":{"action":"search","query":command,"engine":"google"}}]

        if any(k in cmd_lc for k in ("hacker news","hackernews","ycombinator","news.ycombinator")):
            return [{"step":1,"agent":"browser_agent",
                     "description":"Fetch top Hacker News stories",
                     "parameters":{"action":"workflow","goal":command}}]

        url_match = re.search(r"https?://[^\s]+", command)
        if url_match:
            url = url_match.group(0)
            return [{"step":1,"agent":"browser_agent",
                     "description":f"Read page: {url}",
                     "parameters":{"action":"scrape","url":url}}]

        if any(k in cmd_lc for k in ["search google","google for","google search",
                                      "search the web","search bing"]):
            query  = re.sub(
                r".*(search google for|google for|search the web for|search bing for|google search)\s*",
                "", cmd_lc, flags=re.IGNORECASE
            ).strip() or command
            engine = "bing" if "bing" in cmd_lc else "google"
            return [{"step":1,"agent":"browser_agent",
                     "description":f"Search {engine}: {query}",
                     "parameters":{"action":"search","query":query,"engine":engine}}]

        dest_match = re.search(
            r"(?:go to|open|visit|navigate to)\s+([\w\-\.]+\.[\w]{2,}[^\s]*)", cmd_lc
        )
        if dest_match:
            url = dest_match.group(1)
            if not url.startswith("http"): url = "https://" + url
            return [{"step":1,"agent":"browser_agent",
                     "description":f"Browser workflow: {command}",
                     "parameters":{"action":"workflow","goal":command,"url":url}}]

        nav_plain = re.search(r"(?:go to|open|visit|navigate to|launch)\s+(\w+)", cmd_lc)
        if nav_plain:
            site_key = nav_plain.group(1).lower()
            if site_key in SITE_MAP:
                return [{"step":1,"agent":"browser_agent",
                         "description":f"Navigate to {site_key.capitalize()}",
                         "parameters":{"action":"workflow","goal":f"open {site_key}",
                                       "url":SITE_MAP[site_key]}}]

        return [{"step":1,"agent":"browser_agent",
                 "description":f"Browser search: {command}",
                 "parameters":{"action":"search","query":command,"engine":"google"}}]

    # ── Strip trailing research steps ─────────────────────────────────────────

    def _strip_trailing_steps(self, plan: list) -> list:
        TERMINAL_AGENTS = {"document_agent", "coding_agent"}
        if not plan: return plan
        last_terminal = -1
        for i, step in enumerate(plan):
            if step.get("agent") in TERMINAL_AGENTS:
                last_terminal = i
        if last_terminal < 0: return plan
        trailing = plan[last_terminal + 1:]
        if not trailing: return plan
        if all(s.get("agent") == "research_agent" for s in trailing):
            return plan[:last_terminal + 1]
        return plan

    # ── Content generator ─────────────────────────────────────────────────────

    async def generate_content(self, command: str, content_type: str = "text") -> str:
        system = (
            "You are a creative writing assistant. Produce the requested content only. "
            "No preamble, no explanation, no AI disclaimers. Begin writing immediately."
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
        return await self.chat(system, "\n\n".join(parts))
