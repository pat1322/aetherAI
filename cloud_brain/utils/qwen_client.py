"""
AetherAI — Qwen API Client  (Stage 5)
Uses Qwen (Alibaba DashScope) via the OpenAI-compatible endpoint.

Stage 5 changes:
  • classify_command(), plan_task(), and answer() all accept an optional
    user_context string that carries the user's stored preferences.
    This is injected by the orchestrator before every call so Qwen always
    knows things like Patrick's preferred language, timezone, etc.
  • memory_agent added to the planner's agent roster and routing rules.
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
        "using browser agent", "use browser agent", "browser agent",
        "search youtube", "on youtube", "youtube for", "find on youtube",
        "youtube search", "youtube video", "youtube channel",
        "wikipedia", "wiki/",
        "open chrome", "open browser", "open firefox",
        "open chrome and", "launch chrome",
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

    def _is_browser_task(self, cmd_lc: str) -> bool:
        return any(k in cmd_lc for k in self.BROWSER_KEYWORDS)

    def _is_memory_task(self, cmd_lc: str) -> bool:
        return any(k in cmd_lc for k in self.MEMORY_KEYWORDS)

    def _is_open_app_command(self, cmd_lc: str) -> bool:
        OPEN_VERBS  = ["open ", "launch ", "start "]
        OFFICE_APPS = ["word", "excel", "powerpoint", "ppt", "notepad", "notepad++"]
        starts_with_open = any(cmd_lc.startswith(v) or f" {v}" in cmd_lc for v in OPEN_VERBS)
        mentions_app     = any(app in cmd_lc for app in OFFICE_APPS)
        return starts_with_open and mentions_app

    # ── Command classifier ────────────────────────────────────────────────────

    async def classify_command(self, command: str, user_context: str = "") -> str:
        cmd_lc = command.lower()

        # Hard-route memory tasks — always "task" (not chat)
        if self._is_memory_task(cmd_lc):
            return "task"
        if any(k in cmd_lc for k in self.CODE_KEYWORDS):
            return "task"
        if self._is_browser_task(cmd_lc):
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
        cmd_lc     = command.lower()
        is_open_app = self._is_open_app_command(cmd_lc)
        is_browser  = self._is_browser_task(cmd_lc)
        is_coding   = any(k in cmd_lc for k in self.CODE_KEYWORDS)
        is_memory   = self._is_memory_task(cmd_lc)

        # Hard-route memory tasks
        if is_memory:
            return [{
                "step": 1, "agent": "memory_agent",
                "description": f"Memory operation: {command}",
                "parameters": {"query": command},
            }]

        # Hard-route browser tasks
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
            "  Use ONLY when user wants a file to download. NEVER if user said 'open [app]'.\n\n"

            "coding_agent — write and save code\n"
            '  {"task": "...", "language": "python|c|c++|javascript|..."}\n\n'

            "automation_agent — control the PC\n"
            '  new_file: {"action":"new_file","parameters":{"app":"notepad|word|excel|powerpoint|notepad++"}}\n'
            '  type:     {"action":"type","parameters":{"text":"__GENERATED_CONTENT__"}}\n'
            '  hotkey:   {"action":"hotkey","parameters":{"keys":["ctrl","s"]}}\n'
            '  wait:     {"action":"wait","parameters":{"ms":2000}}\n\n'

            "memory_agent — save/recall/forget user preferences and personal facts\n"
            '  {"query": "full user statement"}\n'
            "  Use for: 'remember that', 'what do you know about me', 'forget my X', 'my preference is'.\n\n"

            "ROUTING RULES:\n"
            "1. YouTube/Wikipedia/Chrome/URLs → browser_agent\n"
            "2. 'open word/excel/ppt and write X' → automation_agent ONLY\n"
            "3. 'create/make a presentation/doc/spreadsheet' → document_agent\n"
            "4. 'write a python/C program' (no app) → coding_agent\n"
            "5. General research/news → research_agent\n"
            "6. NEVER write long content inline in JSON — use __GENERATED_CONTENT__\n"
            "7. 'remember/recall/forget/what do you know about me' → memory_agent\n"
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

            return plan
        except (json.JSONDecodeError, ValueError):
            return [{
                "step": 1, "agent": "research_agent",
                "description": f"Process: {command}",
                "parameters": {"query": command},
            }]

    def _build_browser_plan(self, command: str, cmd_lc: str) -> list[dict]:
        if "youtube" in cmd_lc:
            query = re.sub(
                r".*(search youtube for|on youtube|youtube for|find.*youtube|youtube search)\s*",
                "", cmd_lc, flags=re.IGNORECASE
            ).strip() or command
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
