"""
AetherAI — Voice Agent  (Stage 7)

Helpers used by POST /voice/text — the Bronny firmware endpoint.
The full WAV-upload pipeline (Paraformer STT) was removed in Stage 7;
Bronny v5.7+ transcribes on-device via Deepgram and sends pre-transcribed
text here.

Exported:
  _voice_summarize(qwen, full_text, question) -> str
      Condenses an agent/LLM response to 1–3 spoken sentences.

  _safe_synthesize(text) -> bytes
      Synthesises speech via edge-tts, returning b"" on failure.
"""

import logging
import re

from utils.qwen_client import QwenClient
from utils.tts_client  import synthesize

logger = logging.getLogger(__name__)


async def _voice_summarize(qwen: QwenClient, full_text: str, question: str) -> str:
    """
    Condense an agent response into 1–3 conversational sentences suitable
    for speaking aloud. Strips markdown, tables, bullet lists, URLs.
    """
    if len(full_text) <= 200:
        return full_text      # already short enough

    try:
        summary = await qwen.chat(
            system_prompt=(
                "You are condensing an AI response for voice playback. "
                "Rules:\n"
                "- Write 1–3 natural spoken sentences only.\n"
                "- No markdown, no bullet points, no lists, no URLs.\n"
                "- Include the most important number or fact.\n"
                "- Sound conversational, like a smart assistant speaking."
            ),
            user_message=(
                f"Original question: {question}\n\n"
                f"Response to condense:\n{full_text[:1500]}"
            ),
            temperature=0.3,
        )
        return summary.strip()
    except Exception as e:
        logger.warning(f"[VoiceAgent] voice_summarize failed ({e}), using truncation")
        clean = re.sub(r"[#*`_\[\]()>\|~]", "", full_text)
        clean = re.sub(r"https?://\S+", "", clean)
        clean = re.sub(r"\s{2,}", " ", clean).strip()
        return clean[:300]


async def _safe_synthesize(text: str) -> bytes:
    """Synthesise speech, returning empty bytes on failure (never raises)."""
    try:
        return await synthesize(text)
    except Exception as e:
        logger.error(f"[VoiceAgent] TTS failed: {e}")
        return b""
