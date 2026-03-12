"""
AetherAI — Coding Agent
Writes code based on the task description.
Output is returned to the main chat and saved to the output/ folder.
"""

import logging
import re
from datetime import datetime
from pathlib import Path

from agents import BaseAgent

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent.parent.parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# Map common language names to file extensions
LANG_EXTENSIONS = {
    "python":     "py",
    "javascript": "js",
    "typescript": "ts",
    "html":       "html",
    "css":        "css",
    "bash":       "sh",
    "shell":      "sh",
    "sql":        "sql",
    "java":       "java",
    "c":          "c",
    "cpp":        "cpp",
    "c++":        "cpp",
    "csharp":     "cs",
    "c#":         "cs",
    "go":         "go",
    "rust":       "rs",
    "php":        "php",
    "ruby":       "rb",
}


def detect_language(task: str, code: str) -> str:
    """Guess the language from the task description or code content."""
    task_lc = task.lower()
    for lang in LANG_EXTENSIONS:
        if lang in task_lc:
            return lang
    # Sniff from code
    if "def " in code or "import " in code or "print(" in code:
        return "python"
    if "function " in code or "const " in code or "let " in code:
        return "javascript"
    if "<html" in code.lower():
        return "html"
    return "python"  # default


class CodingAgent(BaseAgent):
    name = "coding_agent"
    description = "Writes and saves code"

    async def run(self, parameters: dict, task_id: str, context: str = "") -> str:
        task = parameters.get("task") or parameters.get("query") or context
        language = parameters.get("language", "").lower()

        logger.info(f"[CodingAgent] Task: {task}")

        # Generate the code
        code = await self.qwen.chat(
            system_prompt=(
                "You are an expert programmer. Write clean, well-commented, working code. "
                "Return ONLY the code itself — no markdown fences, no explanation before or after. "
                "Just the raw code."
            ),
            user_message=f"Write code for: {task}",
        )

        # Strip markdown fences if Qwen added them anyway
        code = re.sub(r"```[\w]*\n?", "", code).strip().rstrip("`").strip()

        # Detect language and pick extension
        if not language:
            language = detect_language(task, code)
        ext = LANG_EXTENSIONS.get(language, "txt")

        # Save to output folder
        slug = re.sub(r"[^\w\s-]", "", task).strip()
        slug = re.sub(r"[\s_-]+", "_", slug)[:40]
        ts   = datetime.now().strftime("%H%M%S")
        fname = f"{slug}_{ts}.{ext}"
        fpath = OUTPUT_DIR / fname
        fpath.write_text(code, encoding="utf-8")

        logger.info(f"[CodingAgent] Saved to output/{fname}")

        # Return both the code AND file location so it shows in chat
        return (
            f"💻 **Code generated** (`{language}`) — saved as `output/{fname}`\n\n"
            f"```{language}\n{code}\n```\n\n"
            f"📁 Full path: {fpath}"
        )
