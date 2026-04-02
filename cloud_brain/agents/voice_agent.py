"""
AetherAI — Voice Agent  (Stage 7 + patch)

Helpers used by POST /voice/text — the Bronny firmware endpoint.

Exported:
  _voice_summarize(qwen, full_text, question) -> str
      Condenses an agent/LLM response to 1–3 spoken sentences.

  _safe_synthesize(text) -> bytes
      Synthesises speech via edge-tts, returning b"" on failure.

  _run_single_agent(agent_name, transcript, qwen, memory) -> str
      Runs a single quick agent (weather/crypto/news/finance) and returns
      the result as a plain string. Called by /voice/text for task-type
      voice commands to get a real data answer before TTS.
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
                "- No markdown, no bullet points, no lists, no URLs, no emojis.\n"
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


async def _run_single_agent(
    agent_name: str,
    transcript: str,
    qwen: QwenClient,
    memory,
) -> str:
    """
    Run one quick agent by name and return its plain-text result.

    Used by /voice/text so Bronny can give real live data answers
    (weather, crypto, news, finance) instead of falling back to the
    LLM's training knowledge.

    Supports: weather_agent, crypto_agent, news_agent, finance_agent.
    Returns "" for any unrecognised agent name so the caller falls
    back to bronny_answer() gracefully.
    """
    from utils.websocket_manager import WebSocketManager

    # Create a minimal ws_manager stub — agents need it injected but
    # voice calls don't stream to any UI session.
    class _NullWsManager:
        async def stream_chunk_to_session(self, *a, **kw): pass
        async def broadcast_task_update(self, *a, **kw): pass

    kw = dict(qwen=qwen, ws_manager=_NullWsManager(), memory=memory)

    try:
        if agent_name == "weather_agent":
            from agents.weather_agent import WeatherAgent
            agent = WeatherAgent(**kw)

        elif agent_name == "crypto_agent":
            from agents.crypto_agent import CryptoAgent
            agent = CryptoAgent(**kw)

        elif agent_name == "news_agent":
            from agents.news_agent import NewsAgent
            agent = NewsAgent(**kw)

        elif agent_name == "finance_agent":
            from agents.finance_agent import FinanceAgent
            agent = FinanceAgent(**kw)

        else:
            logger.warning(f"[VoiceAgent] _run_single_agent: unsupported agent '{agent_name}'")
            return ""

        # Give the agent a dummy task_id so internal logging doesn't crash
        result = await agent.run(
            parameters={"query": transcript},
            task_id="voice-inline",
            context=transcript,
        )
        return result or ""

    except Exception as e:
        logger.error(f"[VoiceAgent] _run_single_agent({agent_name}) failed: {e}")
        return ""
