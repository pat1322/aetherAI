"""
AetherAI — Memory Agent  (Stage 5 — patch 2)

FIX 8  _INDEX_KEY changed from "pref:__index__" to "pref:@@index@@".
       The old key could be reached by a user saying "remember my
       __index__ is foo" because _slug strips non-word chars and produces
       "__index__". The new key uses @@ which _slug strips completely,
       making it unreachable via any user input and protecting the index.

Previous fixes retained:
  FIX 2  _forget() / _recall() call memory.delete_preference(key) (real DELETE).
  FIX 4  Duplicate "everything" entry in _WIPE_ALL_WORDS removed.
"""

import json
import logging
import re
from typing import Optional

from agents import BaseAgent

logger = logging.getLogger(__name__)

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

# FIX 8: @@ characters are stripped by _slug → can never be produced by user input
_INDEX_KEY   = "pref:@@index@@"

_WIPE_ALL_WORDS = frozenset(["all", "everything", "all preferences"])


def _slug(text: str) -> str:
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
    return "recall"


class MemoryAgent(BaseAgent):
    name        = "memory_agent"
    description = "Saves and retrieves user preferences and personal facts"

    async def run(self, parameters: dict, task_id: str, context: str = "") -> Optional[str]:
        query = (parameters.get("query") or parameters.get("text")
                 or parameters.get("input") or context or "").strip()

        if not query:
            return await self._recall_all()

        parsed = await self._qwen_parse(query)
        intent = parsed.get("intent", _detect_intent(query))
        label  = parsed.get("label", "")
        value  = parsed.get("value", "")
        topic  = parsed.get("topic", query)

        logger.info(f"[MemoryAgent] intent={intent} label={label!r} value={value!r}")

        if intent == "save" and label and value:
            return await self._save(label, value, raw=query)
        elif intent == "save" and (label or value):
            return await self._save_raw(query)
        elif intent == "forget":
            return await self._forget(topic)
        else:
            return await self._recall(topic)

    async def _qwen_parse(self, text: str) -> dict:
        prompt = f"""Analyse this user statement about a personal preference or fact:
"{text}"

Return ONLY valid JSON — no fences, no extra text:
{{
  "intent": "save" | "recall" | "forget",
  "label": "Short human-readable label for the fact (e.g. 'Preferred language', 'Railway URL')",
  "value": "The actual value being stored (e.g. 'Python', 'https://...')",
  "topic": "Key topic word(s) for searching existing facts (e.g. 'language', 'railway')"
}}

If intent is 'recall' or 'forget', label and value may be empty strings."""
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

    def _get_index(self) -> list[str]:
        raw = self.memory.get_preference(_INDEX_KEY, default=[])
        if isinstance(raw, list):
            return raw
        return []

    def _set_index(self, index: list[str]):
        self.memory.set_preference(_INDEX_KEY, index)

    async def _save(self, label: str, value: str, raw: str = "") -> str:
        key   = _PREF_PREFIX + _slug(label)
        entry = {"label": label, "value": value, "raw": raw}
        self.memory.set_preference(key, entry)

        index = self._get_index()
        if key not in index:
            index.append(key)
            self._set_index(index)

        logger.info(f"[MemoryAgent] Saved: {key} = {value!r}")
        return f"✅ Remembered: **{label}** → {value}"

    async def _save_raw(self, text: str) -> str:
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
        matches  = []
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

        lines  = "\n".join(f"• **{lbl}**: {val}" for lbl, val in matches)
        header = "Here's what I know about you:" if not topic_lc else f"Here's what I know about '{topic}':"
        return f"{header}\n\n{lines}"

    async def _recall_all(self) -> str:
        return await self._recall("")

    async def _forget(self, topic: str) -> str:
        topic_lc = topic.lower().strip()
        index    = self._get_index()
        removed  = []

        if topic_lc in _WIPE_ALL_WORDS:
            for key in list(index):
                self.memory.delete_preference(key)
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
                self.memory.delete_preference(key)
                removed.append(lbl or key)
            else:
                new_index.append(key)

        self._set_index(new_index)

        if removed:
            return f"✅ Forgot: {', '.join(removed)}"
        return f"I don't have anything stored about '{topic}'."

    @staticmethod
    def load_context(memory) -> str:
        """
        Called by the orchestrator before every plan/chat.
        Returns a compact string of all stored preferences.
        """
        try:
            raw   = memory.get_preference(_INDEX_KEY, default=[])
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
