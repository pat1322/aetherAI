"""
AetherAI — Coding Agent
Writes code, shows it in the main chat with syntax highlighting,
and saves it to the output/ folder for download.
Steps panel shows summary only.
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

    # Check task description first — most reliable signal
    if " c " in f" {task_lc} " or task_lc.startswith("c ") or " c program" in task_lc or "in c\b" in task_lc:
        return "c"
    if "c++" in task_lc:   return "cpp"
    if "python" in task_lc: return "python"
    if "java" in task_lc and "javascript" not in task_lc: return "java"
    if "javascript" in task_lc or "js" in task_lc: return "javascript"
    if "typescript" in task_lc: return "typescript"
    if "html" in task_lc:  return "html"
    if "css" in task_lc:   return "css"
    if "bash" in task_lc or "shell" in task_lc: return "bash"
    if "sql" in task_lc:   return "sql"
    if "rust" in task_lc:  return "rust"
    if "go " in task_lc:   return "go"
    if "php" in task_lc:   return "php"
    if "ruby" in task_lc:  return "ruby"

    # Sniff from code content
    if "#include" in code and ("int main" in code or "void main" in code):
        return "c"
    if "def " in code or "print(" in code or "import " in code:
        return "python"
    if "function " in code or "const " in code or "console.log" in code:
        return "javascript"
    if "<html" in code.lower():
        return "html"

    return "python"  # safe default


class CodingAgent(BaseAgent):
    name = "coding_agent"
    description = "Writes and saves code, displays it in the chat"

    async def run(self, parameters: dict, task_id: str, context: str = "") -> str:
        task     = parameters.get("task") or parameters.get("query") or context
        language = parameters.get("language", "").lower().strip()

        logger.info(f"[CodingAgent] Task: {task} | Language hint: {language or 'auto'}")

        # Build prompt with explicit language instruction if given
        lang_instruction = f"Write the code in {language}." if language else ""

        code = await self.qwen.chat(
            system_prompt=(
                "You are an expert programmer. Write clean, well-commented, working code. "
                "Return ONLY the raw source code — no markdown fences, no preamble, "
                "no explanation before or after the code."
            ),
            user_message=f"Write code for: {task}\n{lang_instruction}",
        )

        # Strip any markdown fences Qwen adds despite instructions
        code = re.sub(r"```[\w]*\n?", "", code).strip().rstrip("`").strip()

        # Detect language
        if not language:
            language = detect_language(task or "", code)
        ext = LANG_EXTENSIONS.get(language, "txt")

        # Save file
        slug  = re.sub(r"[^\w\s-]", "", task or "code").strip()
        slug  = re.sub(r"[\s_-]+", "_", slug)[:40]
        ts    = datetime.now().strftime("%H%M%S")
        fname = f"{slug}_{ts}.{ext}"
        fpath = OUTPUT_DIR / fname
        fpath.write_text(code, encoding="utf-8")

        lines = len(code.splitlines())
        logger.info(f"[CodingAgent] Saved {fname} ({lines} lines, {language})")

        # Return format:
        # Line 1 = summary (shown in steps panel)
        # [CODE_BLOCK] = full code (rendered in chat by orchestrator/UI)
        return (
            f"✅ {language.upper()} code ({lines} lines) — saved as output/{fname}\n"
            f"[CODE_BLOCK:{language}]\n{code}\n[/CODE_BLOCK]"
        )
