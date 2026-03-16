"""
AetherAI — STT Client  (Stage 6 — Layer 2)

Uses DashScope's Paraformer ASR model via the OpenAI-compatible
audio/transcriptions endpoint — same API key as Qwen (QWEN_API_KEY),
no separate billing, no extra env var needed.

Model: paraformer-v2
  • Supports WAV, MP3, M4A, FLAC, OGG
  • Up to 60 seconds of audio
  • Returns transcript text

DashScope compatible endpoint:
  https://dashscope-intl.aliyuncs.com/compatible-mode/v1
  → POST /audio/transcriptions   (same shape as OpenAI Whisper)
"""

import logging
from io import BytesIO

from openai import AsyncOpenAI
from config import settings

logger = logging.getLogger(__name__)

# Re-use the DashScope base URL already configured in settings.
# The compatible-mode endpoint lives at the same root.
_DASHSCOPE_BASE = settings.QWEN_BASE_URL.rstrip("/")
# e.g. "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

_ASR_MODEL = "sensevoice-v1"   # multilingual: English, Filipino, Chinese, etc.


def _make_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=settings.QWEN_API_KEY,
        base_url=_DASHSCOPE_BASE,
    )


async def transcribe(audio_bytes: bytes, filename: str = "audio.wav",
                     language: str = "en") -> str:
    """
    Transcribe audio bytes using Qwen Paraformer (DashScope).

    Args:
        audio_bytes: Raw audio file bytes (WAV, MP3, M4A, etc.)
        filename:    Filename hint — determines MIME type sent to the API.
                     Use "audio.wav" for WAV, "audio.mp3" for MP3, etc.
        language:    BCP-47 language code hint, e.g. "en", "fil", "zh".

    Returns:
        Transcript string, or raises RuntimeError on failure.
    """
    if not audio_bytes:
        raise ValueError("audio_bytes is empty — nothing to transcribe")

    client = _make_client()

    # Determine MIME type from filename extension
    ext  = filename.rsplit(".", 1)[-1].lower()
    mime = {
        "wav":  "audio/wav",
        "mp3":  "audio/mpeg",
        "m4a":  "audio/m4a",
        "flac": "audio/flac",
        "ogg":  "audio/ogg",
        "webm": "audio/webm",
    }.get(ext, "audio/wav")

    logger.info(
        f"[STTClient] Transcribing {len(audio_bytes):,} bytes "
        f"({filename}, {mime}) via Paraformer"
    )

    try:
        # The openai client expects a file-like tuple: (filename, bytes, mime)
        response = await client.audio.transcriptions.create(
            model=_ASR_MODEL,
            file=(filename, BytesIO(audio_bytes), mime),
            language=language,
        )
        transcript = response.text.strip()
        logger.info(f"[STTClient] Transcript ({len(transcript)} chars): {transcript[:80]}")
        return transcript

    except Exception as e:
        logger.error(f"[STTClient] Transcription failed: {e}", exc_info=True)
        raise RuntimeError(f"STT failed: {e}") from e
