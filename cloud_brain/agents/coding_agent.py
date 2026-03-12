"""
AetherAI — Coding Agent
Writes code based on the task description.
Full code is shown in main chat. Steps panel shows summary only.
"""

import logging
import re
from datetime import datetime
from pathlib import Path

from agents import BaseAgent

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent.parent.parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

LANG_EXTENSIONS = {
    "python": "py", "javascript": "js", "typescript": "ts",
    "html": "html", "css": "css", "bash": "sh", "shell": "sh",
    "sql": "sql", "java": "java", "c": "c", "cpp": "cpp",
    "c++": "cpp", "csharp": "cs", "c#": "cs", "go": "go",
    "rust": "rs", "php": "php", "ruby": "rb",
}


def detect_language(task: str, code: str) -> str:
    task_lc = task.lower()
    for lang in LANG_EXTENSIONS:
        if lang in task_lc:
            return lang
    if "#include" in code:                              return "c"
    if "def " in code or "print(" in code:             return "python"
    if "function " in code or "const " in code:        return "javascript"
    if "<html" in code.lower():                         return "html"
    return "python"


class CodingAgent(BaseAgent):
    name = "coding_agent"
    description = "Writes and saves code"

    async def run(self, parameters: dict, task_id: str, context: str = "") -> str:
        task     = parameters.get("task") or parameters.get("query") or context
        language = parameters.get("language", "").lower()

        logger.info(f"[CodingAgent] Task: {task}")

        code = await self.qwen.chat(
            system_prompt=(
                "You are an expert programmer. Write clean, well-commented, working code. "
                "Return ONLY the raw code — no markdown fences, no preamble, no explanation."
            ),
            user_message=f"Write code for: {task}",
        )

        # Strip any markdown fences Qwen sneaks in
        code = re.sub(r"```[\w]*\n?", "", code).strip().rstrip("`").strip()

        if not language:
            language = detect_language(task, code)
        ext = LANG_EXTENSIONS.get(language, "txt")

        slug  = re.sub(r"[^\w\s-]", "", task or "code").strip()
        slug  = re.sub(r"[\s_-]+", "_", slug)[:40]
        ts    = datetime.now().strftime("%H%M%S")
        fname = f"{slug}_{ts}.{ext}"
        fpath = OUTPUT_DIR / fname
        fpath.write_text(code, encoding="utf-8")

        logger.info(f"[CodingAgent] Saved to output/{fname}")

        lines = len(code.splitlines())

        # [CODE_BLOCK] is parsed by orchestrator.py to render nicely in chat
        # The steps panel only sees the first line (summary)
        return (
            f"✅ {language} code ({lines} lines) saved as output/{fname}\n"
            f"[CODE_BLOCK:{language}]\n{code}\n[/CODE_BLOCK]"
        )
