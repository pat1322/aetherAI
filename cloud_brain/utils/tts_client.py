"""
AetherAI — TTS Client  (Stage 6 — Layer 2)

Uses edge-tts (Microsoft Azure neural voices, free, no API key).
Returns MP3 bytes directly — ready to stream to the ESP32 or browser.

Default voice: en-US-AriaNeural (configurable via TTS_VOICE env var)

Other good voices:
  en-US-GuyNeural       — Male, neutral American
  en-US-JennyNeural     — Female, friendly American
  en-GB-SoniaNeural     — Female, British
  en-AU-NatashaNeural   — Female, Australian
  fil-PH-BlessicaNeural — Female, Filipino/Tagalog
  fil-PH-AngeloNeural   — Male, Filipino/Tagalog

Full list:  python -m edge_tts --list-voices
"""

import logging
import re

import edge_tts

from config import settings

logger = logging.getLogger(__name__)

# Maximum characters to synthesise in one call — anything longer is truncated
# to keep TTS latency under ~2 seconds for typical voice responses.
_TTS_MAX_CHARS = 600


def _clean_for_speech(text: str) -> str:
    """
    Strip markdown and code that shouldn't be read aloud.
    Keeps natural sentence flow.
    """
    # Remove code blocks entirely
    text = re.sub(r"```[\s\S]*?```", "code block.", text)
    text = re.sub(r"`[^`]+`", "", text)
    # Remove markdown URLs — keep link text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Remove bare URLs
    text = re.sub(r"https?://\S+", "link.", text)
    # Remove markdown symbols
    text = re.sub(r"[#*_~|>]", "", text)
    # Remove table pipes and dashes
    text = re.sub(r"[-]{3,}", ".", text)
    # Collapse multiple spaces/newlines
    text = re.sub(r"\n{2,}", ". ", text)
    text = re.sub(r"\n", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


async def synthesize(text: str, voice: str = "") -> bytes:
    """
    Convert text to MP3 speech using edge-tts.

    Args:
        text:  The text to synthesise. Markdown is stripped automatically.
        voice: edge-tts voice name. Defaults to settings.TTS_VOICE
               (env var TTS_VOICE, default "en-US-AriaNeural").

    Returns:
        MP3 audio bytes, ready to send to the ESP32 or browser.

    Raises:
        RuntimeError on synthesis failure.
    """
    voice     = voice or settings.TTS_VOICE
    clean     = _clean_for_speech(text)[:_TTS_MAX_CHARS]

    if not clean:
        logger.warning("[TTSClient] Nothing to synthesise after cleaning")
        return b""

    logger.info(
        f"[TTSClient] Synthesising {len(clean)} chars "
        f"with voice '{voice}'"
    )

    try:
        communicate = edge_tts.Communicate(clean, voice)
        mp3_chunks: list[bytes] = []

        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_chunks.append(chunk["data"])

        mp3_bytes = b"".join(mp3_chunks)
        logger.info(f"[TTSClient] MP3 ready: {len(mp3_bytes):,} bytes")
        return mp3_bytes

    except Exception as e:
        logger.error(f"[TTSClient] Synthesis failed: {e}", exc_info=True)
        raise RuntimeError(f"TTS failed: {e}") from e


async def list_voices() -> list[dict]:
    """Helper — return all available edge-tts voices for the settings panel."""
    try:
        voices = await edge_tts.list_voices()
        return [
            {
                "name":     v["ShortName"],
                "locale":   v["Locale"],
                "gender":   v["Gender"],
                "friendly": v.get("FriendlyName", v["ShortName"]),
            }
            for v in voices
        ]
    except Exception as e:
        logger.warning(f"[TTSClient] list_voices failed: {e}")
        return []
