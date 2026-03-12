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
    """Async client for Qwen API via DashScope OpenAI-compatible endpoint."""

    def __init__(self):
        if not settings.QWEN_API_KEY:
            raise ValueError(
                "QWEN_API_KEY is not set. "
                "Open your .env file and add: QWEN_API_KEY=sk-..."
            )
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
            "Examples of CHAT: 'who are you', 'what is the capital of France', "
            "'hello', 'what is 2+2', 'local holidays in Philippines'\n\n"
            "'task' = commands that require creating files, doing research and saving results, "
            "controlling the computer, writing code, or multi-step work.\n"
            "Examples of TASK: 'create a presentation about X', 'research X and save findings', "
            "'open Chrome and go to youtube', 'open notepad and type hello', "
            "'write a python program', 'code a calculator'\n\n"
            "Return ONLY the single word: chat\n"
            "Or the single word: task\n"
            "Nothing else."
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
            "- step: integer (1, 2, 3...)\n"
            "- agent: one of [research_agent, document_agent, browser_agent, coding_agent, automation_agent]\n"
            "- description: what this step does (one sentence)\n"
            "- parameters: a dict of relevant inputs for this step\n\n"
            "Return ONLY a valid JSON array of steps. No explanation. No markdown. No extra text.\n\n"

            "Available agents:\n\n"

            "- research_agent: searches the web and summarizes information\n"
            '  parameters: {"query": "search query"}\n\n'

            "- document_agent: creates files. ALWAYS include 'type' in parameters.\n"
            '  parameters: {"type": "presentation", "topic": "..."} <- for PowerPoint (.pptx)\n'
            '  parameters: {"type": "document", "topic": "..."}     <- for Word (.docx)\n'
            '  parameters: {"type": "spreadsheet", "topic": "..."}  <- for Excel (.xlsx)\n'
            "  RULES:\n"
            "  - ONLY use document_agent if the user explicitly asks to CREATE, MAKE, GENERATE, or WRITE a file\n"
            '  - If the user says "presentation", "slides", "PowerPoint", "deck" -> type MUST be "presentation"\n'
            '  - If the user says "spreadsheet", "excel", "data table" -> type MUST be "spreadsheet"\n'
            '  - Otherwise -> type is "document"\n'
            "  - NEVER omit the 'type' field for document_agent\n"
            "  - NEVER call document_agent more than ONCE per task\n\n"

            "- coding_agent: writes code and returns it in the chat output\n"
            '  parameters: {"task": "describe what to code", "language": "python"}\n'
            "  Use coding_agent when user asks to write/create/code a program, script, or function.\n"
            "  The code will be shown in the main chat AND saved to the output folder.\n\n"

            "- browser_agent: controls a real browser (Chrome)\n\n"

            "- automation_agent: controls mouse and keyboard on the connected PC\n"
            "  IMPORTANT: Use 'new_file' action to open a fresh document before typing.\n"
            '  parameters: {"action": "new_file", "parameters": {"app": "notepad"}}  <- new blank notepad\n'
            '  parameters: {"action": "new_file", "parameters": {"app": "word"}}     <- new blank Word doc\n'
            '  parameters: {"action": "new_file", "parameters": {"app": "excel"}}    <- new blank Excel\n'
            '  parameters: {"action": "new_file", "parameters": {"app": "powerpoint"}} <- new blank PPT\n'
            '  parameters: {"action": "open_app", "parameters": {"app": "notepad"}}  <- open app (no new file)\n'
            '  parameters: {"action": "type", "parameters": {"text": "hello"}}       <- type text\n'
            '  parameters: {"action": "hotkey", "parameters": {"keys": ["ctrl", "s"]}}  <- save\n'
            '  parameters: {"action": "run_command", "parameters": {"command": "dir"}}  <- shell command\n'
            "  NEVER use 'open' -- always use 'open_app' or 'new_file'\n"
            "  NEVER use 'press' -- always use 'hotkey'\n"
            "  NEVER use 'write' -- always use 'type'\n"
            "  For Microsoft Word/Excel/PowerPoint: use 'new_file' with app='word'/'excel'/'powerpoint'\n"
        )

        raw = await self.chat(system_prompt, f'Plan this task: "{command}"', temperature=0.3)
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

        try:
            plan = json.loads(raw)
            if not isinstance(plan, list):
                raise ValueError("Plan is not a list")
            return plan
        except (json.JSONDecodeError, ValueError):
            return [{
                "step": 1,
                "agent": "research_agent",
                "description": f"Process command: {command}",
                "parameters": {"query": command},
            }]

    async def summarize(self, content: str, context: str = "") -> str:
        system = "You are a concise summarizer. Summarize the provided content clearly and briefly."
        user = f"{context}\n\nContent to summarize:\n{content}" if context else content
        return await self.chat(system, user, temperature=0.5)

    async def answer(self, question: str, context: str = "") -> str:
        system = (
            "You are AetherAI, a personal AI agent assistant built by Patrick Perez, "
            "a 26-year-old software engineer living in the Philippines. "
            "You are NOT a medical AI. You are a personal productivity and automation assistant. "
            "You help Patrick and his team with research, creating documents, automating tasks, "
            "and controlling computers. "
            "Answer clearly and directly. If asked who you are or who made you, "
            "always say Patrick Perez built you."
        )
        user = f"Context:\n{context}\n\nQuestion: {question}" if context else question
        return await self.chat(system, user)
