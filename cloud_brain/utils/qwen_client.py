"""
AetherAI -- Qwen API Client
Uses Qwen (Alibaba DashScope) via the OpenAI-compatible endpoint.
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
        """Returns 'chat', 'task', or 'code_task'."""
        cmd_lc = command.lower()

        # Hard-coded pre-classifier for coding commands — never let these fall to research_agent
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

    async def plan_task(self, command: str) -> list[dict]:
        """
        IMPORTANT DESIGN RULE:
        The planner must NEVER put long generated content (stories, code, letters)
        inline inside the JSON plan. That causes Qwen to truncate mid-sentence.

        Instead:
        - For 'open app + write content' tasks: plan uses a PLACEHOLDER "__GENERATED_CONTENT__"
          in the type step. The orchestrator replaces it after calling the content generator.
        - For coding tasks WITHOUT an app: use coding_agent standalone.
        - For coding tasks WITH an app (open notepad++ and write code): use coding_agent first,
          then automation_agent type with context from previous step.
        """

        cmd_lc = command.lower()

        # ── Detect if this is a "write code in app" task ──────────────────────
        WRITE_IN_APP = any(k in cmd_lc for k in [
            "notepad", "word", "excel", "powerpoint", "ppt", "notepad++"
        ])
        IS_CODING = any(k in cmd_lc for k in [
            "code", "program", "script", "function", "algorithm",
            "python", " c ", " c++", "java", "javascript", "html"
        ])

        # ── Detect plain text writing in app (no code) ────────────────────────
        IS_WRITING = any(k in cmd_lc for k in [
            "write", "type", "story", "letter", "essay", "song", "poem",
            "note", "article", "report"
        ])

        system_prompt = (
            "You are AetherAI's task planner built by Patrick Perez.\n"
            "Return ONLY a valid JSON array of steps. No explanation. No markdown fences.\n\n"
            "Each step: {step, agent, description, parameters}\n\n"

            "AGENTS:\n\n"

            "research_agent — web search\n"
            '  {"query": "..."}\n\n'

            "document_agent — create .pptx/.docx/.xlsx files (NOT for PC automation)\n"
            '  {"type": "presentation"|"document"|"spreadsheet", "topic": "..."}\n'
            "  Use ONLY when user says CREATE/MAKE/GENERATE a file to download.\n\n"

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

            "RULES:\n"
            "1. NEVER write stories, code, letters, or long text inline in the plan JSON.\n"
            "   Use __GENERATED_CONTENT__ as placeholder in type steps.\n"
            "2. For 'open notepad and write a story/letter/essay':\n"
            "   → automation_agent new_file notepad\n"
            "   → automation_agent type with text='__GENERATED_CONTENT__'\n"
            "3. For 'write a python/C/java program' (no app):\n"
            "   → coding_agent only\n"
            "4. For 'open notepad++ and write code for X':\n"
            "   → coding_agent (generates the code)\n"
            "   → automation_agent new_file notepad++\n"
            "   → automation_agent type with text='__GENERATED_CONTENT__'\n"
            "5. For Word/Excel/PowerPoint: use new_file then wait 3000ms then type\n"
            "6. NEVER use 'open' — use 'open_app' or 'new_file'\n"
            "7. NEVER use 'press' — use 'hotkey'\n"
            "8. NEVER use 'write' as action — use 'type'\n"
            "9. research_agent is ONLY for web searches, NEVER for coding tasks\n"
        )

        raw = await self.chat(system_prompt, f'Plan this task: "{command}"', temperature=0.2)
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

    # ── Content generator (used by orchestrator for __GENERATED_CONTENT__) ───

    async def generate_content(self, command: str, content_type: str = "text") -> str:
        """Generate the actual content for a type step."""
        system = (
            "You are a helpful writing assistant. "
            "Write the requested content clearly and completely. "
            "Return ONLY the content itself — no titles, no labels, no explanation."
        )
        return await self.chat(system, command, temperature=0.7)

    # ── Utility ───────────────────────────────────────────────────────────────

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
