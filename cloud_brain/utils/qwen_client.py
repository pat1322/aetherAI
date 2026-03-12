"""
AetherAI -- Qwen API Client
Uses Qwen (Alibaba DashScope) via the OpenAI-compatible endpoint.
"""

import json
import re
from typing import Optional

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

    async def classify_command(self, command: str) -> str:
        system = (
            "You are a command classifier for AetherAI. "
            "Classify the user input as either 'chat' or 'task'.\n\n"
            "'chat' = simple questions, greetings, factual queries, math, definitions.\n"
            "Examples of CHAT: 'who are you', 'what is the capital of France', 'hello', 'what is 2+2'\n\n"
            "'task' = anything that requires creating files, research, controlling the computer, "
            "writing/generating code, or multi-step work.\n"
            "Examples of TASK: 'create a presentation', 'open notepad and write', "
            "'write a python program', 'code a calculator', 'open word and write a letter'\n\n"
            "Return ONLY the single word: chat OR task. Nothing else."
        )
        result = await self.chat(system, command, temperature=0.0)
        return "chat" if "chat" in result.strip().lower() else "task"

    async def plan_task(self, command: str) -> list[dict]:
        system_prompt = (
            "You are AetherAI's task planner. "
            "AetherAI is a personal AI agent built by Patrick Perez, "
            "a 26-year-old software engineer from the Philippines.\n"
            "Break the user's command into a clear step-by-step execution plan.\n\n"
            "For each step output a JSON object with:\n"
            "- step: integer\n"
            "- agent: one of [research_agent, document_agent, browser_agent, coding_agent, automation_agent]\n"
            "- description: one sentence\n"
            "- parameters: dict of inputs\n\n"
            "Return ONLY a valid JSON array. No explanation. No markdown.\n\n"

            "AGENT GUIDE:\n\n"

            "research_agent — web search + summarize\n"
            '  {"query": "search query"}\n\n'

            "document_agent — create .pptx/.docx/.xlsx files\n"
            '  {"type": "presentation"|"document"|"spreadsheet", "topic": "..."}\n'
            "  ONLY use when user explicitly says CREATE/MAKE/GENERATE a file.\n"
            "  NEVER call more than once.\n\n"

            "coding_agent — write code, show in chat, save to output/\n"
            '  {"task": "what to code", "language": "python|c|javascript|..."}\n'
            "  Use for: 'write a program', 'code a script', 'create a function'\n"
            "  WITHOUT an app mentioned — coding_agent handles it standalone.\n\n"

            "automation_agent — control PC mouse/keyboard\n"
            "  ACTION TYPES:\n"
            '  new_file  — open a fresh blank document (ALWAYS use before typing in an app)\n'
            '    {"action": "new_file", "parameters": {"app": "notepad"|"word"|"excel"|"powerpoint"}}\n'
            '  open_app  — just open an app without creating new file\n'
            '    {"action": "open_app", "parameters": {"app": "chrome"|"notepad"|...}}\n'
            '  type      — type text into the active window\n'
            '    {"action": "type", "parameters": {"text": "the full text to type"}}\n'
            '  hotkey    — press key combination\n'
            '    {"action": "hotkey", "parameters": {"keys": ["ctrl", "s"]}}\n'
            '  click     — click at x,y coordinates\n'
            '    {"action": "click", "parameters": {"x": 500, "y": 300}}\n'
            '  run_command — run shell command\n'
            '    {"action": "run_command", "parameters": {"command": "dir"}}\n'
            '  wait      — pause\n'
            '    {"action": "wait", "parameters": {"ms": 2000}}\n\n'

            "CRITICAL RULES:\n"
            "- NEVER use 'open' — always use 'open_app' or 'new_file'\n"
            "- NEVER use 'press' — always use 'hotkey'\n"
            "- NEVER use 'write' — always use 'type'\n"
            "- When user says 'open X and type/write Y' — use new_file then type\n"
            "- When user says 'open notepad++ and write code' — use:\n"
            "    step 1: coding_agent to generate the code\n"
            "    step 2: automation_agent new_file with app='notepad++' \n"
            "    step 3: automation_agent type with the generated code\n"
            "- For Word/Excel/PowerPoint: always use new_file (not open_app) then type\n"
            "- Add a wait step (2000ms) after opening Office apps before typing\n"
            "- When user says 'open word and write X': new_file word → wait 2000ms → type X\n"
        )

        raw = await self.chat(system_prompt, f'Plan this task: "{command}"', temperature=0.3)
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

        try:
            plan = json.loads(raw)
            if not isinstance(plan, list):
                raise ValueError("Not a list")
            return plan
        except (json.JSONDecodeError, ValueError):
            return [{
                "step": 1, "agent": "research_agent",
                "description": f"Process: {command}",
                "parameters": {"query": command},
            }]

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
