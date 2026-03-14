"""
AetherAI — Memory Agent  (Stage 5)

The memory agent gives AetherAI a persistent "brain" about the user.
Patrick can say things like:

  "Remember that I prefer Python over JavaScript"
  "My Railway URL is https://aetherai.up.railway.app"
  "What do you know about me?"
  "Forget my Railway URL"
  "Clear all preferences"

The agent reads the intent, then reads/writes the preferences table in
memory.py via MemoryManager.  It is injected into every planning and
chat call by the orchestrator so that Qwen always has context about
Patrick's preferences when generating plans and answers.

INTENT TYPES
────────────
  save   — store a key/value fact ("remember that …", "my X is Y")
  recall — retrieve all facts, or search for one topic
  forget  — delete a specific key or all keys

STORAGE FORMAT
──────────────
  Each preference is stored as:
    key:   "pref:<slug>"   e.g. "pref:preferred_language"
    value: {"label": "Preferred language", "value": "Python", "raw": original sentence}

  A special key "pref:__index__" stores a list of all pref keys so
  we can list them without a LIKE query (memory.py uses key-exact lookup).
"""

import json
import logging
import re
from typing import Optional

from agents import BaseAgent

logger = logging.getLogger(__name__)

# ── Intent patterns ────────────────────────────────────────────────────────────
_SAVE_TRIGGERS = [
    r"\bremember\b", r"\bstore\b", r"\bsave\b", r"\bnote that\b",
    r"\bmy .+ is\b", r"\bi (prefer|use|like|want|am|have)\b",
    r"\bset .+ to\b", r"\bmy preference\b",
]
_RECALL_TRIGGERS = [
    r"\bwhat do you know\b", r"\bwhat have you remembered\b",
    r"\bshow (my |all )?(preferences|memories|facts)\b",
    r"\blist (my |all )?(preferences|memories|facts)\b",
    r"\brecall\b", r"\bdo you (know|remember)\b",
    r"\bwhat is my\b", r"\bwhat('s| is) my\b",
]
_FORGET_TRIGGERS = [
    r"\bforget\b", r"\bdelete\b", r"\bremove\b", r"\bclear\b",
    r"\bwipe\b", r"\bdiscard\b",
]

_PREF_PREFIX = "pref:"
_INDEX_KEY   = "pref:__index__"


def _slug(text: str) -> str:
    """Convert free text to a stable key slug."""
    s = re.sub(r"[^\w\s]", "", text.lower()).strip()
    s = re.sub(r"\s+", "_", s)
    return s[:60]


def _detect_intent(text: str) -> str:
    tl = text.lower()
    for pat in _FORGET_TRIGGERS:
        if re.search(pat, tl): return "forget"
    for pat in _SAVE_TRIGGERS:
        if re.search(pat, tl): return "save"
    for pat in _RECALL_TRIGGERS:
        if re.search(pat, tl): return "recall"
    return "recall"   # default: show what we know


class MemoryAgent(BaseAgent):
    name        = "memory_agent"
    description = "Saves and retrieves user preferences and personal facts"

    # ── Public interface ───────────────────────────────────────────────────────

    async def run(self, parameters: dict, task_id: str, context: str = "") -> Optional[str]:
        query = (parameters.get("query") or parameters.get("text")
                 or parameters.get("input") or context or "").strip()

        if not query:
            return await self._recall_all()

        # Let Qwen parse the intent and extract ALL facts in the statement
        parsed = await self._qwen_parse(query)
        intent = parsed.get("intent", _detect_intent(query))
        facts  = parsed.get("facts", [])   # list of {label, value} dicts
        topic  = parsed.get("topic", query)

        logger.info(f"[MemoryAgent] intent={intent} facts={facts}")

        if intent == "save":
            if facts:
                # Save every fact extracted from the sentence
                results = []
                for fact in facts:
                    label = fact.get("label", "").strip()
                    value = fact.get("value", "").strip()
                    if label and value:
                        result = await self._save(label, value, raw=query)
                        results.append(result)
                if results:
                    return "\n".join(results)
            # Fallback: nothing parsed cleanly
            return await self._save_raw(query)
        elif intent == "forget":
            return await self._forget(topic)
        else:
            return await self._recall(topic)

    # ── Qwen-assisted parsing ──────────────────────────────────────────────────

    async def _qwen_parse(self, text: str) -> dict:
        prompt = f"""Analyse this user statement about personal preferences or facts:
"{text}"

A single sentence may contain MULTIPLE facts (e.g. "my name is X and I live in Y" → two facts).
Extract ALL of them.

Return ONLY valid JSON — no fences, no extra text:
{{
  "intent": "save" | "recall" | "forget",
  "facts": [
    {{"label": "Short human-readable label (e.g. 'Full name')", "value": "The actual value (e.g. 'Patrick Perez')"}},
    {{"label": "Location", "value": "Baliuag, Bulacan, Philippines"}}
  ],
  "topic": "Key topic word(s) for searching/forgetting (e.g. 'language', 'name')"
}}

Rules:
- For intent "save": populate "facts" with ALL label/value pairs found.
- For intent "recall" or "forget": "facts" can be an empty list [].
- Labels should be concise: "Full name", "Location", "Preferred language", "Railway URL", etc.
- Values should be the exact thing stated by the user."""
        try:
            raw = await self.qwen.chat(
                system_prompt="You extract structured facts from natural language. Return ONLY valid JSON.",
                user_message=prompt,
                temperature=0.0,
            )
            raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
            return json.loads(raw)
        except Exception as e:
            logger.warning(f"[MemoryAgent] Qwen parse failed: {e}")
            return {}

    # ── CRUD helpers ───────────────────────────────────────────────────────────

    def _get_index(self) -> list[str]:
        raw = self.memory.get_preference(_INDEX_KEY, default=[])
        if isinstance(raw, list):
            return raw
        return []

    def _set_index(self, index: list[str]):
        self.memory.set_preference(_INDEX_KEY, index)

    async def _save(self, label: str, value: str, raw: str = "") -> str:
        key = _PREF_PREFIX + _slug(label)
        entry = {"label": label, "value": value, "raw": raw}
        self.memory.set_preference(key, entry)

        # Maintain index
        index = self._get_index()
        if key not in index:
            index.append(key)
            self._set_index(index)

        logger.info(f"[MemoryAgent] Saved: {key} = {value!r}")
        return f"✅ Remembered: **{label}** → {value}"

    async def _save_raw(self, text: str) -> str:
        """Fallback: store the whole sentence under a slug of the text."""
        label = text[:60]
        slug  = _slug(text[:40])
        key   = _PREF_PREFIX + slug
        entry = {"label": label, "value": text, "raw": text}
        self.memory.set_preference(key, entry)
        index = self._get_index()
        if key not in index:
            index.append(key)
            self._set_index(index)
        return f"✅ Noted: {text[:80]}"

    async def _recall(self, topic: str = "") -> str:
        index = self._get_index()
        if not index:
            return "I don't have any preferences or facts stored yet. Tell me something about yourself and I'll remember it."

        topic_lc = topic.lower()
        matches = []
        for key in index:
            entry = self.memory.get_preference(key)
            if not entry or not isinstance(entry, dict):
                continue
            lbl = entry.get("label", "")
            val = entry.get("value", "")
            if not topic_lc or topic_lc in lbl.lower() or topic_lc in val.lower():
                matches.append((lbl, val))

        if not matches:
            return f"I don't have anything stored about '{topic}'. Try asking me to remember something first."

        lines = "\n".join(f"• **{lbl}**: {val}" for lbl, val in matches)
        header = "Here's what I know about you:" if not topic_lc else f"Here's what I know about '{topic}':"
        return f"{header}\n\n{lines}"

    async def _recall_all(self) -> str:
        return await self._recall("")

    async def _forget(self, topic: str) -> str:
        topic_lc = topic.lower().strip()
        index    = self._get_index()
        removed  = []

        # "all" / "everything" — wipe all prefs
        if topic_lc in ("all", "everything", "all preferences", "everything"):
            for key in list(index):
                self.memory.set_preference(key, None)  # None = effectively deleted
            self._set_index([])
            return "✅ All preferences cleared."

        new_index = []
        for key in index:
            entry = self.memory.get_preference(key)
            if not entry or not isinstance(entry, dict):
                continue
            lbl = entry.get("label", "")
            val = entry.get("value", "")
            if topic_lc in lbl.lower() or topic_lc in val.lower() or topic_lc in key.lower():
                # Mark as deleted by setting to None
                self.memory.set_preference(key, None)
                removed.append(lbl or key)
            else:
                new_index.append(key)

        self._set_index(new_index)

        if removed:
            return f"✅ Forgot: {', '.join(removed)}"
        return f"I don't have anything stored about '{topic}'."

    # ── Static method for external callers ────────────────────────────────────

    @staticmethod
    def load_context(memory) -> str:
        """
        Called by the orchestrator before every plan/chat.
        Returns a compact string of all stored preferences to inject
        into Qwen's system prompt.
        Returns empty string if no preferences exist.
        """
        try:
            raw = memory.get_preference(_INDEX_KEY, default=[])
            index = raw if isinstance(raw, list) else []
            if not index:
                return ""

            lines = []
            for key in index:
                entry = memory.get_preference(key)
                if not entry or not isinstance(entry, dict):
                    continue
                lbl = entry.get("label", "")
                val = entry.get("value", "")
                if lbl and val:
                    lines.append(f"- {lbl}: {val}")

            if not lines:
                return ""

            return "User preferences and facts:\n" + "\n".join(lines)
        except Exception as e:
            logger.warning(f"[MemoryAgent.load_context] {e}")
            return ""
