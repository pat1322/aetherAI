"""
AetherAI — Qwen API Client  (Stage 4 — hardened)

WHAT'S NEW vs the previous version
────────────────────────────────────
1. Plan caching (LRU, 64 slots, 5-minute TTL)
   Identical commands within 5 minutes skip the Qwen plan call entirely.

2. Context passed to planner
   plan_task() accepts optional `context` so the planner sees prior step
   outputs and can make smarter decisions in multi-step flows.

3. Better open-chrome routing
   More destination patterns: "play X on youtube", "open twitch.tv",
   "open <domain.tld>" regex. KNOWN_SITES dict expanded.
   URL builder handles bare "youtube" with no query.

4. Cleaner plan JSON repair
   Unwraps {"plan":[...]}, {"steps":[...]} before falling back to research.

5. generate_content() preamble stripping
   Regex strips all common AI preambles, not just a fixed list.
   Retries once if result is suspiciously short.

6. CancelledError pass-through in chat()
   Ensures the orchestrator's real cancellation propagates correctly.
"""

import json
import re
import time
import hashlib
from collections import OrderedDict
from typing import Optional
from urllib.parse import quote_plus

from openai import AsyncOpenAI
from config import settings


# ── LRU cache with TTL ────────────────────────────────────────────────────────

class _TTLCache:
    def __init__(self, maxsize: int = 64, ttl: float = 300.0):
        self._cache: OrderedDict[str, tuple] = OrderedDict()
        self._maxsize = maxsize
        self._ttl     = ttl

    def _key(self, text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()

    def get(self, text: str) -> Optional[object]:
        k = self._key(text)
        entry = self._cache.get(k)
        if entry is None:
            return None
        value, expires = entry
        if time.monotonic() > expires:
            del self._cache[k]
            return None
        self._cache.move_to_end(k)
        return value

    def set(self, text: str, value: object):
        k = self._key(text)
        if k in self._cache:
            self._cache.move_to_end(k)
        self._cache[k] = (value, time.monotonic() + self._ttl)
        while len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)


_plan_cache = _TTLCache(maxsize=64, ttl=300.0)

# ── Keyword sets ──────────────────────────────────────────────────────────────

OPEN_CHROME_KEYWORDS = [
    "open chrome", "launch chrome", "start chrome",
    "open browser", "launch browser",
    "open firefox", "launch firefox",
    "go to youtube", "go to reddit", "go to twitter", "go to facebook",
    "go to instagram", "go to twitch", "go to github", "go to google",
    "go to hacker news", "go to hackernews", "go to stackoverflow",
    "go to linkedin", "go to tiktok", "go to netflix", "go to spotify",
    "play on youtube", "watch on youtube", "play on twitch", "watch on twitch",
    "navigate to", "browse to",
]

BROWSER_KEYWORDS = [
    "using browser agent", "use browser agent", "browser agent",
    "search youtube for", "on youtube", "youtube for", "find on youtube",
    "youtube search", "youtube video", "youtube channel",
    "wikipedia", "wiki/",
    "search google for", "google for", "google search",
    "search the web for", "search bing",
    "scrape", "extract from", "read the article at",
    "summarize the page", "summarize the website", "summarize the article at",
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

OFFICE_APPS = ["word", "excel", "powerpoint", "ppt", "notepad", "notepad++"]

KNOWN_SITES: dict[str, str] = {
    "youtube":       "youtube.com",
    "reddit":        "reddit.com",
    "twitter":       "twitter.com",
    "x":             "x.com",
    "facebook":      "facebook.com",
    "instagram":     "instagram.com",
    "twitch":        "twitch.tv",
    "github":        "github.com",
    "stackoverflow": "stackoverflow.com",
    "google":        "google.com",
    "hacker news":   "news.ycombinator.com",
    "hackernews":    "news.ycombinator.com",
    "ycombinator":   "news.ycombinator.com",
    "linkedin":      "linkedin.com",
    "tiktok":        "tiktok.com",
    "netflix":       "netflix.com",
    "spotify":       "open.spotify.com",
    "wikipedia":     "wikipedia.org",
    "amazon":        "amazon.com",
    "gmail":         "mail.google.com",
    "discord":       "discord.com",
}

_PREAMBLE_RE = re.compile(
    r"^(sure[!,.]?|certainly[!,.]?|of course[!,.]?|absolutely[!,.]?|"
    r"here(?:'s| is)(?: the| a| your)?[^:]*:|great[!,.]?|"
    r"i(?:'ll| will) (?:write|create|generate|help)[^.]*\.|"
    r"no problem[!,.]?|happy to help[!,.]?)\s*",
    re.IGNORECASE | re.MULTILINE,
)


# ── Client ────────────────────────────────────────────────────────────────────

class QwenClient:

    def __init__(self):
        if not settings.QWEN_API_KEY:
            raise ValueError("QWEN_API_KEY is not set.")
        self._client = AsyncOpenAI(
            api_key=settings.QWEN_API_KEY,
            base_url=settings.QWEN_BASE_URL,
        )
        self.model = settings.QWEN_MODEL

    async def chat(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.7,
    ) -> str:
        import asyncio
        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_message},
                ],
                temperature=temperature,
            )
            return response.choices[0].message.content.strip()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise

    # ── Routing helpers ───────────────────────────────────────────────────────

    def _is_open_chrome_command(self, cmd_lc: str) -> bool:
        if any(k in cmd_lc for k in OPEN_CHROME_KEYWORDS):
            return True
        # "open <domain.tld>" e.g. "open twitch.tv"
        if re.search(r"\bopen\s+[\w\-]+\.\w{2,}", cmd_lc):
            return True
        return False

    def _is_browser_task(self, cmd_lc: str) -> bool:
        if self._is_open_chrome_command(cmd_lc):
            return False
        return any(k in cmd_lc for k in BROWSER_KEYWORDS)

    def _is_open_app_command(self, cmd_lc: str) -> bool:
        OPEN_VERBS = ["open ", "launch ", "start "]
        starts_with_open = any(cmd_lc.startswith(v) or f" {v}" in cmd_lc for v in OPEN_VERBS)
        mentions_app = any(app in cmd_lc for app in OFFICE_APPS)
        return starts_with_open and mentions_app

    # ── Classifier ────────────────────────────────────────────────────────────

    async def classify_command(self, command: str) -> str:
        cmd_lc = command.lower()
        if any(k in cmd_lc for k in CODE_KEYWORDS):
            return "task"
        if self._is_open_chrome_command(cmd_lc):
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

    # ── Planner ───────────────────────────────────────────────────────────────

    async def plan_task(self, command: str, context: str = "") -> list[dict]:
        cmd_lc = command.lower()

        # Hard routes — never call Qwen
        if self._is_open_chrome_command(cmd_lc):
            return self._build_open_chrome_plan(command, cmd_lc)
        if self._is_browser_task(cmd_lc):
            return self._build_browser_plan(command, cmd_lc)

        # Plan cache
        cache_key = command.strip().lower()
        cached = _plan_cache.get(cache_key)
        if cached is not None:
            import copy
            return copy.deepcopy(cached)

        is_open_app = self._is_open_app_command(cmd_lc)

        no_doc_rule = (
            "\n⚠️ CRITICAL: This command says 'open [app]'. "
            "You MUST use automation_agent. NEVER use document_agent.\n"
            if is_open_app else ""
        )
        context_block = f"\nPrevious context:\n{context[:800]}\n" if context else ""

        system_prompt = (
            "You are AetherAI's task planner built by Patrick Perez.\n"
            "Return ONLY a valid JSON array of steps. No explanation. No markdown fences.\n\n"
            + no_doc_rule
            + context_block +
            "Each step: {step, agent, description, parameters}\n\n"

            "AGENTS:\n\n"

            "research_agent — DuckDuckGo + Qwen summarization\n"
            '  {"query": "..."}\n'
            "  Use for: general research, facts, news. NOT for YouTube/Chrome/URLs.\n\n"

            "browser_agent — headless Chromium (Playwright)\n"
            '  {"action":"search","query":"...","engine":"duckduckgo"}\n'
            '  {"action":"youtube","query":"..."}\n'
            '  {"action":"scrape","url":"https://..."}\n'
            '  {"action":"workflow","goal":"...","url":"https://..."}\n'
            "  Use for: YouTube research, Wikipedia, URL scraping.\n\n"

            "document_agent — DOWNLOADABLE .pptx/.docx/.xlsx files\n"
            '  {"type":"presentation"|"document"|"spreadsheet","topic":"..."}\n'
            "  Use ONLY when user wants a downloadable file. NEVER if user said 'open [app]'.\n\n"

            "coding_agent — write and save code\n"
            '  {"task":"...","language":"python|c|c++|javascript|..."}\n\n'

            "automation_agent — control the PC\n"
            '  {"action":"new_file","parameters":{"app":"notepad|word|excel|powerpoint"}}\n'
            '  {"action":"type","parameters":{"text":"__GENERATED_CONTENT__"}}\n'
            '  {"action":"hotkey","parameters":{"keys":["ctrl","s"]}}\n'
            '  {"action":"wait","parameters":{"ms":2000}}\n\n'

            "RULES:\n"
            "1. YouTube/Wikipedia/scraping → browser_agent\n"
            "2. 'open word/excel and write X' → automation_agent ONLY\n"
            "3. 'create a presentation/doc/spreadsheet' → document_agent\n"
            "4. 'write a python/C program' → coding_agent\n"
            "5. General research → research_agent\n"
            "6. NEVER inline long text in JSON — use __GENERATED_CONTENT__\n"
        )

        raw = await self.chat(system_prompt, f'Plan this task: "{command}"', temperature=0.2)
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

        plan = self._parse_plan_json(raw, command)

        if is_open_app:
            plan = [s for s in plan if s.get("agent") != "document_agent"]

        for idx, step in enumerate(plan, 1):
            step["step"] = idx

        _plan_cache.set(cache_key, plan)
        return plan

    # ── JSON repair ───────────────────────────────────────────────────────────

    @staticmethod
    def _parse_plan_json(raw: str, command: str) -> list[dict]:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                for key in ("plan", "steps", "actions", "result"):
                    if isinstance(parsed.get(key), list):
                        parsed = parsed[key]
                        break
            if isinstance(parsed, list) and parsed:
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass

        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
                if isinstance(parsed, list) and parsed:
                    return parsed
            except Exception:
                pass

        return [{
            "step": 1, "agent": "research_agent",
            "description": f"Process: {command}",
            "parameters": {"query": command},
        }]

    # ── Open-chrome plan ──────────────────────────────────────────────────────

    def _build_open_chrome_plan(self, command: str, cmd_lc: str) -> list[dict]:
        destination = self._extract_chrome_destination(command, cmd_lc)

        steps: list[dict] = [
            {
                "step": 1,
                "agent": "automation_agent",
                "description": "Open Chrome browser",
                "parameters": {"action": "open_app", "parameters": {"app": "chrome"}},
            },
            {
                "step": 2,
                "agent": "automation_agent",
                "description": "Wait for Chrome to load",
                "parameters": {"action": "wait", "parameters": {"ms": 2500}},
            },
        ]

        if destination:
            steps.append({
                "step": 3,
                "agent": "automation_agent",
                "description": f"Navigate to {destination}",
                "parameters": {
                    "action": "run_command",
                    "parameters": {"command": f'start "" "{destination}"'},
                },
            })

        return steps

    def _extract_chrome_destination(self, command: str, cmd_lc: str) -> Optional[str]:
        # Explicit URL
        url_m = re.search(r"https?://[^\s]+", command)
        if url_m:
            return url_m.group(0)

        # "open <domain.tld>"
        domain_m = re.search(r"\bopen\s+([\w\-]+\.\w{2,})", cmd_lc)
        if domain_m:
            return "https://" + domain_m.group(1)

        # "go to / navigate to / browse to <site>"
        nav_m = re.search(
            r"\b(?:go to|navigate to|browse to|open|visit)\s+([\w\s\-\.]+?)(?:\s+and|\s*$)",
            cmd_lc,
        )
        if nav_m:
            dest = nav_m.group(1).strip().rstrip(".")
            for name, domain in KNOWN_SITES.items():
                if name in dest:
                    return f"https://{domain}"
            if re.match(r"[\w\-]+\.\w{2,}", dest):
                return "https://" + dest

        # "google <query>"
        google_m = re.search(r"\bgoogle\s+(.+)", cmd_lc)
        if google_m:
            return f"https://www.google.com/search?q={quote_plus(google_m.group(1).strip())}"

        # "play/watch X on youtube"
        yt_m = re.search(
            r"(?:play|watch|search youtube for|on youtube)\s+(.+)", cmd_lc
        )
        if yt_m:
            q = re.sub(r"\s+(on youtube|on chrome)$", "", yt_m.group(1)).strip()
            if q:
                return f"https://www.youtube.com/results?search_query={quote_plus(q)}"
            return "https://www.youtube.com"

        # Bare site name
        for name, domain in KNOWN_SITES.items():
            if name in cmd_lc:
                return f"https://{domain}"

        return None

    # ── Browser plan (headless) ───────────────────────────────────────────────

    def _build_browser_plan(self, command: str, cmd_lc: str) -> list[dict]:
        if "youtube" in cmd_lc:
            query = re.sub(
                r".*(search youtube for|on youtube|youtube for|find.*youtube|youtube search)\s*",
                "", cmd_lc, flags=re.IGNORECASE,
            ).strip() or command
            return [{
                "step": 1, "agent": "browser_agent",
                "description": f"Search YouTube: {query}",
                "parameters": {"action": "youtube", "query": query},
            }]

        if "wikipedia" in cmd_lc or "wiki/" in cmd_lc:
            url_m = re.search(r"(https?://[^\s]+|wikipedia\.org/wiki/[^\s]+)", cmd_lc)
            if url_m:
                url = url_m.group(1)
                if not url.startswith("http"):
                    url = "https://" + url
                return [{
                    "step": 1, "agent": "browser_agent",
                    "description": f"Read Wikipedia: {url}",
                    "parameters": {"action": "scrape", "url": url},
                }]
            return [{
                "step": 1, "agent": "browser_agent",
                "description": f"Search Wikipedia: {command}",
                "parameters": {"action": "search", "query": command, "engine": "google"},
            }]

        url_m = re.search(r"https?://[^\s]+", command)
        if url_m:
            return [{
                "step": 1, "agent": "browser_agent",
                "description": f"Read: {url_m.group(0)}",
                "parameters": {"action": "scrape", "url": url_m.group(0)},
            }]

        if any(k in cmd_lc for k in [
            "search google for", "google for", "google search",
            "search the web for", "search bing for",
        ]):
            query = re.sub(
                r".*(search google for|google for|search the web for|search bing for|google search)\s*",
                "", cmd_lc, flags=re.IGNORECASE,
            ).strip() or command
            engine = "bing" if "bing" in cmd_lc else "google"
            return [{
                "step": 1, "agent": "browser_agent",
                "description": f"Search {engine}: {query}",
                "parameters": {"action": "search", "query": query, "engine": engine},
            }]

        dest_m = re.search(
            r"(?:go to|open|visit|navigate to)\s+([\w\-\.]+\.[\w]{2,}[^\s]*)", cmd_lc
        )
        if dest_m:
            url = dest_m.group(1)
            if not url.startswith("http"):
                url = "https://" + url
            return [{
                "step": 1, "agent": "browser_agent",
                "description": f"Browser workflow: {command}",
                "parameters": {"action": "workflow", "goal": command, "url": url},
            }]

        return [{
            "step": 1, "agent": "browser_agent",
            "description": f"Search: {command}",
            "parameters": {"action": "search", "query": command, "engine": "duckduckgo"},
        }]

    # ── Content generator ─────────────────────────────────────────────────────

    async def generate_content(self, command: str, content_type: str = "text") -> str:
        system = (
            "You are a creative writing assistant. Output ONLY the raw content — "
            "no preamble, no explanation. Begin writing immediately."
        )
        clean = re.sub(
            r"\b(open|launch|start|create|use)\s+"
            r"(notepad\+\+|notepad|word|excel|powerpoint|ppt|a file|a new file|an?\s+app)"
            r"\s*(and|then|to)?\s*",
            "", command, flags=re.IGNORECASE,
        ).strip() or command

        result = await self.chat(system, f"Write this: {clean}", temperature=0.7)
        result = _PREAMBLE_RE.sub("", result).strip()

        if len(result) < 30:
            result = await self.chat(system, f"Write this content now: {clean}", temperature=0.8)
            result = _PREAMBLE_RE.sub("", result).strip()

        return result

    # ── Utilities ─────────────────────────────────────────────────────────────

    async def summarize(self, content: str, context: str = "") -> str:
        system = "You are a concise, accurate summarizer. Summarize clearly and briefly."
        user   = f"{context}\n\nContent:\n{content}" if context else content
        return await self.chat(system, user, temperature=0.5)

    async def answer(self, question: str, context: str = "") -> str:
        system = (
            "You are AetherAI, a personal AI agent assistant built by Patrick Perez, "
            "a 26-year-old software engineer from the Philippines. "
            "Answer clearly and concisely. Do not mention that you are an AI unless asked."
        )
        user = f"Context:\n{context}\n\nQuestion: {question}" if context else question
        return await self.chat(system, user)
