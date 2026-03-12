"""
AetherAI — Research Agent
Searches the web and summarizes results using Qwen.
Stage 1: uses DuckDuckGo (no API key needed). More sources added later.
"""

import logging
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from agents import BaseAgent
from utils.qwen_client import QwenClient

logger = logging.getLogger(__name__)

DDGO_URL = "https://html.duckduckgo.com/html/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


class ResearchAgent(BaseAgent):
    name = "research_agent"
    description = "Searches the web and summarizes information"

    def __init__(self, qwen: QwenClient):
        super().__init__(qwen)

    async def run(self, parameters: dict, task_id: str, context: str = "") -> Optional[str]:
        query = parameters.get("query") or context or "general research"
        logger.info(f"[ResearchAgent] Query: {query}")

        # ── 1. Fetch search results ───────────────────────────────────────────
        raw_results = await self._search_duckduckgo(query)

        if not raw_results:
            # Fallback: ask Qwen from its training knowledge
            logger.warning("[ResearchAgent] No web results, using Qwen knowledge")
            return await self.qwen.answer(
                question=query,
                context="Answer from your training knowledge since web search returned no results.",
            )

        # ── 2. Summarize with Qwen ────────────────────────────────────────────
        combined = "\n\n".join(raw_results[:5])  # top 5 snippets
        summary = await self.qwen.summarize(
            content=combined,
            context=f"Research query: {query}\nSummarize the key findings clearly and concisely.",
        )
        logger.info(f"[ResearchAgent] Summary complete ({len(summary)} chars)")
        return summary

    async def _search_duckduckgo(self, query: str) -> list[str]:
        """Returns a list of text snippets from DuckDuckGo HTML search."""
        try:
            async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15) as client:
                resp = await client.post(DDGO_URL, data={"q": query, "b": ""})
                resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")
            results = []

            for result in soup.select(".result__body"):
                snippet = result.get_text(separator=" ", strip=True)
                if snippet:
                    results.append(snippet[:800])

            logger.info(f"[ResearchAgent] Found {len(results)} snippets")
            return results

        except Exception as e:
            logger.error(f"[ResearchAgent] Search error: {e}")
            return []
