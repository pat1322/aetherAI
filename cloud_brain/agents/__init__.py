"""
AetherAI — Base Agent  (Stage 6 — streaming patch)

Stage 6 streaming additions
────────────────────────────
set_stream_context(task_id)
    Called by AgentRouter before every agent.run() so the agent knows
    which task it belongs to for streaming purposes.

stream_llm(system, user, temperature)
    Drop-in replacement for self.qwen.chat() that streams tokens to the
    UI via ws_manager.stream_chunk_to_ui() if a stream context is set.
    Falls back to blocking self.qwen.chat() if no context (e.g. called
    from vision loop, voice agent, or unit tests).

stream_summarize(content, context, source)
    Streaming equivalent of self.qwen.summarize(). Builds the same
    system prompts as QwenClient.summarize() then calls stream_llm()
    so the summary appears token-by-token instead of all at once.
    Used by browser_agent and research_agent for their final write steps.
"""

from abc import ABC, abstractmethod
from typing import Optional

from utils.qwen_client import QwenClient


class BaseAgent(ABC):
    name: str        = "base_agent"
    description: str = "Base agent"

    def __init__(self, qwen: QwenClient, ws_manager=None, memory=None, **kwargs):
        self.qwen       = qwen
        self.ws_manager = ws_manager
        self.memory     = memory
        # Set by AgentRouter before each run() call
        self._stream_task_id:    Optional[str] = None
        self._stream_session_id: str            = ""

    def set_stream_context(self, task_id: str, session_id: str = ""):
        """
        Called by AgentRouter before agent.run() so the agent knows
        which WebSocket task and session to stream chunks to.
        session_id ensures chunks only go to the user who made the request.
        """
        self._stream_task_id      = task_id
        self._stream_session_id   = session_id

    async def stream_llm(self, system_prompt: str, user_message: str,
                         temperature: float = 0.7) -> str:
        """
        Streaming drop-in for self.qwen.chat().

        If a stream context is set (task_id + ws_manager available):
          - Streams tokens via ws_manager.stream_chunk_to_ui()
          - Returns the accumulated full text when done

        If no stream context (voice agent, unit tests, vision loop):
          - Falls back silently to blocking self.qwen.chat()
          - Returns the full text as normal
        """
        if self.ws_manager and self._stream_task_id:
            full = ""
            async for chunk in self.qwen.stream_chat(
                system_prompt, user_message, temperature
            ):
                full += chunk
                await self.ws_manager.stream_chunk_to_session(
                    self._stream_session_id, self._stream_task_id, chunk
                )
            return full
        else:
            return await self.qwen.chat(system_prompt, user_message, temperature)

    async def stream_summarize(self, content: str, context: str = "",
                               source: str = "") -> str:
        """
        Streaming equivalent of self.qwen.summarize().

        Builds the same system prompts as QwenClient.summarize() then
        routes through stream_llm() so the summary streams to the UI
        token-by-token. Used by browser_agent and research_agent for
        their final write/summarize steps.
        """
        if source == "web":
            system = (
                "You are an information analyst. Content was JUST fetched live from the web.\n"
                "NEVER say 'as of my knowledge cutoff' or 'I cannot access real-time data'.\n"
                "Base your summary ENTIRELY on the provided content.\n"
                "Be thorough — include all key facts, numbers, names, and specific details.\n"
                "Use clear structure with headings and bullet points.\n"
                "CRITICAL: Use ONLY plain markdown. NEVER output HTML tags like <a>, <br>, <p>. "
                "For links use markdown: [title](url) format only."
            )
        else:
            system = (
                "You are a thorough content analyst. "
                "Summarize in detail — include all key facts and important details. "
                "Use clear structure. Do not truncate.\n"
                "CRITICAL: Use ONLY plain markdown. NEVER output HTML tags."
            )
        user = f"{context}\n\nContent:\n{content}" if context else content
        return await self.stream_llm(system, user, temperature=0.4)

    @abstractmethod
    async def run(
        self,
        parameters: dict,
        task_id: str,
        context: str = "",
    ) -> Optional[str]: ...
