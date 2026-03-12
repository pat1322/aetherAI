"""
AetherAI — Qwen API Client
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
                "QWEN_API_KEY is not set in environment variables.\n"
                "Open your .env file and make sure it has: QWEN_API_KEY=sk-..."
            )
        self._client = AsyncOpenAI(
            api_key=settings.QWEN_API_KEY,
            base_url=settings.QWEN_BASE_URL,
        )
        self.model = settings.QWEN_MODEL

    async def chat(self, system_prompt: str, user_message: str, temperature: float = 0.7) -> str:
        """
        Simple chat completion using Qwen.
        Returns the assistant's reply as a string.
        """
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
        """
        Classify whether a command is a simple question (answer directly)
        or a real task (needs agents). Returns "chat" or "task".
        """
        system = """You are a command classifier for AetherAI, a personal AI agent owned by Patrick Perez.
Classify the user input as either "chat" or "task".

"chat" = simple questions, greetings, factual queries, math, definitions, personal questions about AetherAI.
Examples of CHAT:
- "who are you", "what is AetherAI", "hello", "who made you"
- "what is the capital of France", "what is 2+2"
- "local holidays in the Philippines", "what time is it in Tokyo"
- "how are you", "what can you do"
- Any question that can be answered with a short text response

"task" = commands that require creating files, doing research and saving results, controlling the computer, or multi-step work.
Examples of TASK:
- "create a presentation about X", "make a report on Y"
- "research X and save the findings", "build me a spreadsheet"
- "open Chrome and go to...", "write a Python script"

Return ONLY the single word: chat
Or the single word: task
Nothing else."""
        result = await self.chat(system, command, temperature=0.0)
        result = result.strip().lower()
        return "chat" if "chat" in result else "task"

    async def plan_task(self, command: str) -> list[dict]:
        """
        Ask Qwen to break a command into an ordered execution plan.
        Returns a list of step dicts: [{step, agent, description, parameters}]
        """
        system_prompt = """You are AetherAI's task planner. AetherAI is a personal AI agent built by Patrick Perez, a 26-year-old software engineer from the Philippines.
Your job is to break a user's command into a clear step-by-step execution plan.

For each step, output a JSON object with:
- step: integer (1, 2, 3...)
- agent: one of [research_agent, document_agent, browser_agent, coding_agent, automation_agent]
- description: what this step does (one sentence)
- parameters: a dict of relevant inputs for this step

Return ONLY a valid JSON array of steps. No explanation. No markdown. No extra text.

Available agents:
- research_agent: searches the web and summarizes information
  parameters: {"query": "search query"}

- document_agent: creates files. ALWAYS include "type" in parameters.
  parameters: {"type": "presentation", "topic": "..."}  ← for PowerPoint (.pptx)
  parameters: {"type": "document", "topic": "..."}      ← for Word (.docx)
  parameters: {"type": "spreadsheet", "topic": "..."}   ← for Excel (.xlsx)
  RULES:
  - ONLY use document_agent if the user explicitly asks to CREATE, MAKE, GENERATE, or WRITE a file
  - If the user says "presentation", "slides", "PowerPoint", "deck" → type MUST be "presentation"
  - If the user says "spreadsheet", "excel", "data table" → type MUST be "spreadsheet"
  - Otherwise → type is "document"
  - NEVER omit the "type" field for document_agent
  - NEVER create a document just because the topic could be a document — user must explicitly ask

- browser_agent: controls a real browser (Chrome)
- coding_agent: writes and runs code
- automation_agent: controls mouse and keyboard on a connected PC
"""
        user_message = f'Plan this task: "{command}"'
        raw = await self.chat(system_prompt, user_message, temperature=0.3)

        # Strip markdown fences if model wraps output
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
                "raw_response": raw,
            }]

    async def summarize(self, content: str, context: str = "") -> str:
        """Summarize a block of text."""
        system = "You are a concise summarizer. Summarize the provided content clearly and briefly."
        user = f"{context}\n\nContent to summarize:\n{content}" if context else content
        return await self.chat(system, user, temperature=0.5)

    async def answer(self, question: str, context: str = "") -> str:
        """Answer a question, optionally with context."""
        system = """You are AetherAI, a personal AI agent assistant built by Patrick Perez — a 26-year-old software engineer living in the Philippines. 
You are NOT a medical AI. You are a personal productivity and automation assistant.
You help Patrick and his team with research, creating documents, automating tasks, and controlling computers.
Answer questions clearly and directly. If asked who you are or who made you, always say Patrick Perez built you."""
        user = f"Context:\n{context}\n\nQuestion: {question}" if context else question
        return await self.chat(system, user)
