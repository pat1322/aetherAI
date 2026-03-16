"""
AetherAI — Coding Agent  (Stage 6 — streaming patch)

Streaming change
────────────────
The primary code generation call now uses self.stream_llm() so code
appears character-by-character in the stream bubble before being
extracted into the final syntax-highlighted code block.

The retry call (on syntax error) also uses stream_llm() so the
corrected code streams too.

All Stage 4 fixes retained:
  FIX 13  Clean two-step alias resolution (no double dict.get(x,x))
"""

import ast
import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from agents import BaseAgent

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent.parent.parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

LANG_EXTENSIONS = {
    "python": "py", "javascript": "js", "typescript": "ts",
    "html": "html", "css": "css", "bash": "sh", "shell": "sh",
    "sql": "sql", "java": "java", "c": "c", "cpp": "cpp",
    "c++": "cpp", "csharp": "cs", "c#": "cs", "go": "go",
    "golang": "go", "rust": "rs", "php": "php", "ruby": "rb",
    "kotlin": "kt", "swift": "swift", "r": "r",
}

_LANG_PATTERNS: list[tuple[str, str]] = [
    (r"\bc\+\+\b",               "cpp"),
    (r"\bcpp\b",                 "cpp"),
    (r"\bcsharp\b|c#",           "csharp"),
    (r"\btypescript\b|\btsx?\b", "typescript"),
    (r"\bjavascript\b|\bjs\b",   "javascript"),
    (r"\bjava\b",                "java"),
    (r"\bpython\b|\bpy\b",       "python"),
    (r"\bkotlin\b",              "kotlin"),
    (r"\bswift\b",               "swift"),
    (r"\bgolang\b|\bgo\b",       "go"),
    (r"\brust\b",                "rust"),
    (r"\bhtml\b",                "html"),
    (r"\bcss\b",                 "css"),
    (r"\bbash\b|\bshell\b",      "bash"),
    (r"\bsql\b",                 "sql"),
    (r"\bphp\b",                 "php"),
    (r"\bruby\b|\brb\b",         "ruby"),
    (r"\br\b",                   "r"),
    (r"\bc\s+program|\bin\s+c\b|write\s+a?\s*c\b", "c"),
]

_ALIASES = {"c++": "cpp", "c#": "csharp", "golang": "go"}


def detect_language(task: str, code: str) -> str:
    task_lc = task.lower()
    for pattern, lang in _LANG_PATTERNS:
        if re.search(pattern, task_lc):
            return lang
    if "#include" in code and re.search(r"\b(int|void)\s+main\s*\(", code):
        return "c"
    if re.search(r"\bdef\s+\w+\s*\(|\bimport\s+\w+|\bprint\s*\(", code):
        return "python"
    if re.search(r"\bfunction\s+\w+|\bconst\s+\w+\s*=|\bconsole\.log\b", code):
        return "javascript"
    if "<html" in code.lower():
        return "html"
    return "python"


def _slug(text: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^\w\s-]", "", text).strip()
    return re.sub(r"[\s_-]+", "_", s)[:maxlen] or "code"


def _validate(language: str, code: str) -> Optional[str]:
    if language == "python":
        try:
            ast.parse(code)
            return None
        except SyntaxError as e:
            return f"SyntaxError line {e.lineno}: {e.msg}"
    if language in ("c", "cpp"):
        if not re.search(r"\b(int|void)\s+main\s*\(", code):
            return "No main() function found"
    return None


def _extract_blocks(raw: str) -> list[tuple[str, str]]:
    matches = re.findall(r"```(\w*)\n?(.*?)```", raw, re.DOTALL)
    if matches:
        return [(l.strip().lower(), c.strip()) for l, c in matches if c.strip()]
    cleaned = raw.strip()
    return [("", cleaned)] if cleaned else []


class CodingAgent(BaseAgent):
    name        = "coding_agent"
    description = "Writes and saves code, displays it in the chat"

    async def run(self, parameters: dict, task_id: str, context: str = "") -> str:
        try:
            return await self._run(parameters, task_id, context)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[CodingAgent] Error: {e}", exc_info=True)
            return f"⚠️ CodingAgent error: {e}"

    async def _run(self, parameters: dict, task_id: str, context: str) -> str:
        task      = parameters.get("task") or parameters.get("query") or context
        lang_hint = parameters.get("language", "").lower().strip()
        lang_hint = _ALIASES.get(lang_hint, lang_hint)

        logger.info(f"[CodingAgent] task={task[:80]} lang_hint={lang_hint or 'auto'}")

        sys_prompt = (
            "You are an expert programmer. Write clean, well-commented, working code. "
            "Return ONLY raw source code or fenced code blocks. "
            "No prose before or after the code."
        )
        lang_instr = f"Write the code in {lang_hint}." if lang_hint else ""
        user_msg   = f"Write code for: {task}\n{lang_instr}"

        # STREAMING — code generation streams token-by-token
        raw    = await self.stream_llm(sys_prompt, user_msg)
        blocks = _extract_blocks(raw)

        if not blocks:
            logger.warning("[CodingAgent] Empty response — retrying")
            raw    = await self.stream_llm(
                sys_prompt,
                user_msg + "\n\nIMPORTANT: Return ONLY source code.",
            )
            blocks = _extract_blocks(raw)

        if not blocks:
            return "⚠️ CodingAgent: Qwen returned no code."

        raw_lang     = lang_hint or blocks[0][0] or detect_language(task or "", blocks[0][1])
        primary_lang = _ALIASES.get(raw_lang, raw_lang) or "python"

        err = _validate(primary_lang, blocks[0][1])
        if err:
            logger.warning(f"[CodingAgent] Validation: {err} — retrying")
            # STREAMING — retry also streams
            raw2    = await self.stream_llm(
                sys_prompt,
                f"{user_msg}\n\nPrevious attempt error: {err}\nFix it. Return ONLY code.",
            )
            blocks2 = _extract_blocks(raw2)
            if blocks2 and blocks2[0][1]:
                blocks = blocks2

        ts   = datetime.now().strftime("%H%M%S")
        slug = _slug(task or "code")
        saved: list[tuple[str, str, str]] = []

        for idx, (blk_lang, blk_code) in enumerate(blocks):
            if not blk_code:
                continue
            raw_blk_lang = lang_hint or blk_lang or detect_language(task or "", blk_code)
            lang         = _ALIASES.get(raw_blk_lang, raw_blk_lang) or "python"
            ext          = LANG_EXTENSIONS.get(lang, "txt")

            suffix = f"_{idx}" if idx > 0 else ""
            fname  = f"{slug}{suffix}_{ts}.{ext}"
            fpath  = OUTPUT_DIR / fname
            fpath.write_text(blk_code, encoding="utf-8")
            saved.append((fname, lang, blk_code))
            logger.info(f"[CodingAgent] Saved {fname} "
                        f"({len(blk_code.splitlines())} lines, {lang})")

        if not saved:
            return "⚠️ CodingAgent: No code blocks could be saved."

        pf, pl, pc = saved[0]
        lines      = len(pc.splitlines())

        if len(saved) == 1:
            summary = (f"✅ {pl.upper()} code ({lines} lines) "
                       f"— saved as output/{pf}")
        else:
            flist   = ", ".join(f"output/{f}" for f, _, _ in saved)
            summary = (f"✅ {len(saved)} files ({pl.upper()} primary, "
                       f"{lines} lines) — {flist}")

        return f"{summary}\n[CODE_BLOCK:{pl}]\n{pc}\n[/CODE_BLOCK]"
