"""
AetherAI — Voice Agent  (Stage 6 — Layer 2)

Orchestrates the full ESP32 voice pipeline:
  1. STT   — Paraformer transcribes WAV audio bytes → text
  2. Route — classify_command() picks the best agent
  3. LLM   — answer() or a single-agent call produces a response
  4. Trim  — voice_summarize() condenses response to ≤ 3 spoken sentences
  5. TTS   — edge-tts synthesises the trimmed text → MP3 bytes

Design decisions for voice:
  • ALL commands go through answer() or single-step agents (no multi-step
    orchestrator plans — the ESP32 can't display step progress and the user
    doesn't want to wait 30 seconds for a task plan).
  • Weather / crypto / finance / news are handled by running their agents
    directly and summarising the result for speech.
  • The response is capped at ~600 chars for TTS so playback stays ≤ 10s.
  • The raw agent output is also returned so the ESP32 TFT can optionally
    display more detail.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from utils.qwen_client  import QwenClient
from utils.stt_client   import transcribe
from utils.tts_client   import synthesize
from memory             import MemoryManager

logger = logging.getLogger(__name__)

# Agents that are safe to call directly for a quick voice answer
_SINGLE_STEP_AGENTS = {
    "weather_agent",
    "crypto_agent",
    "news_agent",
    "finance_agent",
}


@dataclass
class VoiceResult:
    transcript:    str   # what the user said
    response_text: str   # full agent/LLM response (for TFT display)
    spoken_text:   str   # condensed ≤ 600 char version (fed to TTS)
    mp3_bytes:     bytes # synthesised audio


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
        # Fallback: strip markdown and truncate
        import re
        clean = re.sub(r"[#*`_\[\]()>\|~]", "", full_text)
        clean = re.sub(r"https?://\S+", "", clean)
        clean = re.sub(r"\s{2,}", " ", clean).strip()
        return clean[:300]


async def _run_single_agent(agent_name: str, query: str,
                             qwen: QwenClient, memory: MemoryManager) -> str:
    """Instantiate and run one agent directly without the full orchestrator."""
    try:
        kw = dict(qwen=qwen, ws_manager=None, memory=memory)
        if agent_name == "weather_agent":
            from agents.weather_agent import WeatherAgent
            return await WeatherAgent(**kw).run({"query": query}, task_id="voice")
        if agent_name == "crypto_agent":
            from agents.crypto_agent import CryptoAgent
            return await CryptoAgent(**kw).run({"query": query}, task_id="voice")
        if agent_name == "news_agent":
            from agents.news_agent import NewsAgent
            return await NewsAgent(**kw).run({"query": query}, task_id="voice")
        if agent_name == "finance_agent":
            from agents.finance_agent import FinanceAgent
            return await FinanceAgent(**kw).run({"query": query}, task_id="voice")
    except Exception as e:
        logger.warning(f"[VoiceAgent] {agent_name} failed: {e}")
    return ""


async def process_voice(
    audio_bytes: bytes,
    qwen:        QwenClient,
    memory:      Optional[MemoryManager] = None,
) -> VoiceResult:
    """
    Full voice pipeline: WAV → transcript → response → MP3.

    Args:
        audio_bytes: Raw WAV audio from the ESP32 (16kHz mono 16-bit).
        qwen:        Shared QwenClient instance.
        memory:      Optional MemoryManager for user context injection.

    Returns:
        VoiceResult with transcript, response text, spoken text, and MP3.
    """

    # ── 1. STT ────────────────────────────────────────────────────────────────
    logger.info(f"[VoiceAgent] Processing {len(audio_bytes):,} byte audio")
    try:
        transcript = await transcribe(audio_bytes)
    except Exception as e:
        logger.error(f"[VoiceAgent] STT failed: {e}")
        error_text  = "Sorry, I couldn't understand the audio. Please try again."
        mp3         = await _safe_synthesize(error_text)
        return VoiceResult(
            transcript="[STT error]",
            response_text=str(e),
            spoken_text=error_text,
            mp3_bytes=mp3,
        )

    if not transcript:
        silence_text = "I didn't catch that. Please speak clearly and try again."
        mp3          = await _safe_synthesize(silence_text)
        return VoiceResult("", silence_text, silence_text, mp3)

    logger.info(f"[VoiceAgent] Heard: '{transcript}'")

    # ── 2. Load user context ──────────────────────────────────────────────────
    user_context = ""
    if memory:
        try:
            from agents.memory_agent import MemoryAgent
            user_context = MemoryAgent.load_context(memory)
        except Exception:
            pass

    # ── 3. Route & generate response ─────────────────────────────────────────
    command_type = await qwen.classify_command(transcript, user_context=user_context)
    logger.info(f"[VoiceAgent] Classified as: {command_type}")

    response_text = ""

    if command_type == "task":
        # Try to match a single-step agent for instant voice-friendly answers
        plan = await qwen.plan_task(transcript, user_context=user_context)
        first_agent = plan[0].get("agent", "") if plan else ""

        if first_agent in _SINGLE_STEP_AGENTS:
            logger.info(f"[VoiceAgent] Running {first_agent} directly")
            response_text = await _run_single_agent(
                first_agent, transcript, qwen, memory
            )

        if not response_text:
            # Fallback for tasks that need multi-step work — answer directly
            # from knowledge rather than running the full orchestrator
            logger.info("[VoiceAgent] Task fallback → direct answer")
            response_text = await qwen.answer(
                transcript, user_context=user_context
            )
    else:
        # Direct chat answer
        response_text = await qwen.answer(transcript, user_context=user_context)

    if not response_text:
        response_text = "I wasn't able to get an answer for that. Please try again."

    # ── 4. Condense for TTS ───────────────────────────────────────────────────
    spoken_text = await _voice_summarize(qwen, response_text, transcript)
    logger.info(f"[VoiceAgent] Spoken text ({len(spoken_text)} chars): {spoken_text[:80]}")

    # ── 5. TTS ────────────────────────────────────────────────────────────────
    mp3_bytes = await _safe_synthesize(spoken_text)

    return VoiceResult(
        transcript=transcript,
        response_text=response_text,
        spoken_text=spoken_text,
        mp3_bytes=mp3_bytes,
    )


async def _safe_synthesize(text: str) -> bytes:
    """Synthesise speech, returning empty bytes on failure (never raises)."""
    try:
        return await synthesize(text)
    except Exception as e:
        logger.error(f"[VoiceAgent] TTS failed: {e}")
        return b""
