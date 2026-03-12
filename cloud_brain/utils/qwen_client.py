"""
AetherAI -- Qwen API Client
Uses Qwen (Alibaba DashScope) via the OpenAI-compatible endpoint.

FIX: plan_task now has a hard pre-check — if the command starts with "open [office app]",
it is treated as a PC automation task and document_agent is NEVER used.
document_agent is only used when the user explicitly asks to CREATE/MAKE/GENERATE a file
to download, without saying "open [app]".
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

    # ── Command classifier ────────────────────────────────────────────────────

    async def classify_command(self, command: str) -> str:
        """Returns 'chat' or 'task'."""
        cmd_lc = command.lower()

        CODE_KEYWORDS = [
            "write a program", "create a program", "make a program",
            "write a script", "create a script",
            "write code", "create code", "generate code",
            "code that", "program that", "script that",
            "write a python", "write a c ", "write a c++", "write a java",
            "write a function", "create a function",
            "write an algorithm", "implement a",
        ]
        if any(k in cmd_lc for k in CODE_KEYWORDS):
            return "task"

        system = (
            "You are a command classifier for AetherAI. "
            "Classify the user input as either 'chat' or 'task'.\n\n"
            "'chat' = simple questions, greetings, factual queries, math, definitions.\n"
            "Examples of CHAT: 'who are you', 'what is 2+2', 'hello', 'what is the capital of France'\n\n"
            "'task' = anything requiring creating files, research, computer control, writing code, "
            "or multi-step work.\n"
            "Examples of TASK: 'create a presentation', 'open notepad and write', "
            "'write a python program', 'open word and write a letter', 'research X'\n\n"
            "Return ONLY the single word: chat OR task. Nothing else."
        )
        result = await self.chat(system, command, temperature=0.0)
        return "chat" if "chat" in result.strip().lower() else "task"

    # ── Task planner ──────────────────────────────────────────────────────────

    def _is_open_app_command(self, cmd_lc: str) -> bool:
        """
        Returns True if the command is asking to OPEN an app on the PC.
        These must ALWAYS use automation_agent — never document_agent.
        """
        OPEN_VERBS = ["open ", "launch ", "start "]
        OFFICE_APPS = ["word", "excel", "powerpoint", "ppt", "notepad", "notepad++"]
        starts_with_open = any(cmd_lc.startswith(v) or f" {v}" in cmd_lc for v in OPEN_VERBS)
        mentions_app = any(app in cmd_lc for app in OFFICE_APPS)
        return starts_with_open and mentions_app

    async def plan_task(self, command: str) -> list[dict]:
        cmd_lc = command.lower()
        is_open_app = self._is_open_app_command(cmd_lc)

        IS_CODING = any(k in cmd_lc for k in [
            "code", "program", "script", "function", "algorithm",
            "python", " c ", " c++", "java", "javascript", "html"
        ])

        # Hard rule: if user says "open [app]", NEVER route to document_agent.
        # document_agent is ONLY for "create/make/generate a file to download".
        no_doc_agent_rule = ""
        if is_open_app:
            no_doc_agent_rule = (
                "\n⚠️ CRITICAL OVERRIDE: This command says 'open [app]'. "
                "You MUST use automation_agent to control the PC. "
                "You MUST NOT use document_agent under any circumstances. "
                "document_agent creates downloadable files — it does NOT open apps.\n"
            )

        system_prompt = (
            "You are AetherAI's task planner built by Patrick Perez.\n"
            "Return ONLY a valid JSON array of steps. No explanation. No markdown fences.\n\n"
            + no_doc_agent_rule +
            "Each step: {step, agent, description, parameters}\n\n"

            "AGENTS:\n\n"

            "research_agent — web search\n"
            '  {"query": "..."}\n\n'

            "document_agent — create DOWNLOADABLE .pptx/.docx/.xlsx files\n"
            '  {"type": "presentation"|"document"|"spreadsheet", "topic": "..."}\n'
            "  Use ONLY when user says CREATE/MAKE/GENERATE a file to download.\n"
            "  NEVER use if the user said 'open [app]'.\n\n"

            "coding_agent — generate code, shows in chat, saves to output/\n"
            '  {"task": "describe what to code", "language": "python|c|c++|javascript|..."}\n'
            "  Use when user wants code WITHOUT opening an app.\n"
            "  Also use as STEP 1 when user wants to write code IN an app.\n\n"

            "automation_agent — control the PC\n"
            '  new_file:    {"action":"new_file","parameters":{"app":"notepad|word|excel|powerpoint|notepad++"}}\n'
            '  open_app:    {"action":"open_app","parameters":{"app":"chrome|notepad|..."}}\n'
            '  type:        {"action":"type","parameters":{"text":"__GENERATED_CONTENT__"}}\n'
            '                 ^^^ USE __GENERATED_CONTENT__ AS PLACEHOLDER — never write long text inline\n'
            '  hotkey:      {"action":"hotkey","parameters":{"keys":["ctrl","s"]}}\n'
            '  wait:        {"action":"wait","parameters":{"ms":2000}}\n\n'

            "ROUTING RULES:\n"
            "1. 'open word/excel/powerpoint and write/create X' → automation_agent ONLY\n"
            "   (new_file → type with __GENERATED_CONTENT__)\n"
            "2. 'create/make/generate a presentation/document/spreadsheet' (no app mention) → document_agent\n"
            "3. 'write a python/C/java program' (no app) → coding_agent only\n"
            "4. 'open notepad++ and write code for X' → coding_agent + new_file + type\n"
            "5. For Word/Excel/PowerPoint automation: new_file then wait 2000ms then type\n"
            "6. NEVER use 'open' as action — use 'open_app' or 'new_file'\n"
            "7. NEVER use 'press' — use 'hotkey'\n"
            "8. NEVER use 'write' as action — use 'type'\n"
            "9. NEVER write stories, code, letters, or long text inline in JSON — use __GENERATED_CONTENT__\n"
            "10. research_agent is ONLY for web searches, NEVER for coding or document tasks\n"
        )

        raw = await self.chat(system_prompt, f'Plan this task: "{command}"', temperature=0.2)
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

        try:
            plan = json.loads(raw)
            if not isinstance(plan, list):
                raise ValueError("Not a list")

            # Safety net: if open_app command still got document_agent, strip it out
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

    # ── Content generator ─────────────────────────────────────────────────────

    async def generate_content(self, command: str, content_type: str = "text") -> str:
        """Generate content for a type step (story, song, letter, essay, etc.)."""
        system = (
            "You are a creative writing assistant. Your ONLY job is to produce the requested written content. "
            "NEVER mention files, apps, computers, or what you can/cannot do. "
            "NEVER start with phrases like 'I can\\'t', 'Here is', 'Sure!', 'Certainly', or any preamble. "
            "NEVER refer to the fact that you are an AI. "
            "Output ONLY the raw content itself — prose, lyrics, letter body, or whatever was requested. "
            "Begin writing immediately with the actual content."
        )
        clean_cmd = re.sub(
            r"\b(open|launch|start|create|use)\s+(notepad\+\+|notepad|word|excel|powerpoint|ppt|a file|a new file|an?\s+app)\s*(and|then|to)?\s*",
            "", command, flags=re.IGNORECASE
        ).strip()
        if not clean_cmd:
            clean_cmd = command
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
