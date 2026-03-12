"""
AetherAI — Research Agent  (Stage 4 — hardened)

WHAT'S NEW vs the previous version
────────────────────────────────────
1. Multiple sources
   Old version: DuckDuckGo only, one POST request, top-5 snippets.
   New version: DuckDuckGo HTML (primary) + DuckDuckGo JSON API (fallback)
   + direct page fetch for the top result URL when snippets are thin.
   Gives richer context to Qwen and reduces "I couldn't find anything"
   responses.

2. Source URLs in output
   The summary now ends with a "Sources:" block listing the actual URLs
   of pages that contributed to the answer. Useful for follow-up and
   gives the user somewhere to dig deeper.

3. Snippet deduplication
   DDG often returns the same snippet text from multiple result entries
   (especially for news). Duplicates are filtered by MD5 hash before
   being passed to Qwen, so the LLM isn't summarising the same sentence
   four times.

4. Query cleaning
   Strips common filler phrases ("tell me about", "what is", "explain",
   "research", "find information on") from the query before sending to
   DDG. Cleaner queries → better result quality.

5. Retry on HTTP errors
   Single retry with 2 s back-off on transient DDG failures (429, 5xx).
   Prevents a single flaky request from falling through to the Qwen
   knowledge-only fallback unnecessarily.

6. Per-URL fetch cap
   Direct page fetches are capped at 3000 chars and timeout at 8 s to
   prevent a slow page from blocking the whole research step.

7. CancelledError pass-through.
"""

import asyncio
import hashlib
import logging
import re
from typing import Optional
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

from agents import BaseAgent

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

DDG_HTML_URL = "https://html.duckduckgo.com/html/"
DDG_JSON_URL = "https://api.duckduckgo.com/?q={q}&format=json&no_html=1&skip_disambig=1"

PAGE_FETCH_LIMIT = 3000
PAGE_FETCH_TIMEOUT = 8.0
MAX_SNIPPETS = 6

# Filler phrases to strip from queries before sending to DDG
_FILLER_RE = re.compile(
    r"^\s*(tell me about|what is|what are|explain|research|"
    r"find information (on|about)|look up|search for|give me info on)\s+",
    re.IGNORECASE,
)


def _clean_query(query: str) -> str:
    return _FILLER_RE.sub("", query).strip()


def _dedup(snippets: list[str]) -> list[str]:
    seen, out = set(), []
    for s in snippets:
        key = hashlib.md5(s[:120].encode()).hexdigest()
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


class ResearchAgent(BaseAgent):
    name        = "research_agent"
    description = "Searches the web and summarises information"

    async def run(self, parameters: dict, task_id: str, context: str = "") -> Optional[str]:
        try:
            return await self._run(parameters, task_id, context)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[ResearchAgent] Error: {e}", exc_info=True)
            return f"⚠️ ResearchAgent error: {e}"

    async def _run(self, parameters: dict, task_id: str, context: str) -> Optional[str]:
        raw_query = parameters.get("query") or context or "general research"
        query     = _clean_query(raw_query)
        logger.info(f"[ResearchAgent] Query: {query}")

        snippets, urls = await self._search(query)

        if not snippets:
            logger.warning("[ResearchAgent] No web results — using Qwen knowledge")
            return await self.qwen.answer(
                question=raw_query,
                context="Answer from your training knowledge; web search returned no results.",
            )

        combined = "\n\n".join(snippets[:MAX_SNIPPETS])
        summary  = await self.qwen.summarize(
            content=combined,
            context=f"Research query: {query}\nSummarise the key findings clearly and concisely.",
        )

        if urls:
            url_block = "\n".join(f"• {u}" for u in urls[:5])
            return f"{summary}\n\n**Sources:**\n{url_block}"

        return summary

    # ── Search pipeline ────────────────────────────────────────────────────────

    async def _search(self, query: str) -> tuple[list[str], list[str]]:
        """
        Returns (snippets, source_urls).
        Tries DDG HTML first, falls back to DDG JSON API, then fetches
        the top result page directly if snippets are very thin.
        """
        snippets, urls = await self._ddg_html(query)

        if len(snippets) < 2:
            logger.info("[ResearchAgent] HTML thin — trying DDG JSON API")
            json_snippets, json_urls = await self._ddg_json(query)
            snippets += json_snippets
            urls     += [u for u in json_urls if u not in urls]

        snippets = _dedup(snippets)

        # If still thin, fetch the top URL directly
        if len(snippets) < 2 and urls:
            logger.info(f"[ResearchAgent] Fetching top URL directly: {urls[0]}")
            page_text = await self._fetch_page(urls[0])
            if page_text:
                snippets.append(page_text)

        logger.info(f"[ResearchAgent] {len(snippets)} snippets from {len(urls)} sources")
        return snippets, urls

    async def _ddg_html(self, query: str) -> tuple[list[str], list[str]]:
        snippets: list[str] = []
        urls:     list[str] = []
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(
                    headers=HEADERS, follow_redirects=True, timeout=15
                ) as client:
                    resp = await client.post(DDG_HTML_URL, data={"q": query, "b": ""})
                    if resp.status_code in (429, 500, 502, 503) and attempt == 0:
                        await asyncio.sleep(2)
                        continue
                    resp.raise_for_status()

                soup = BeautifulSoup(resp.text, "lxml")

                for result in soup.select(".result"):
                    # Extract snippet text
                    body = result.select_one(".result__body, .result__snippet")
                    if body:
                        text = body.get_text(separator=" ", strip=True)
                        if len(text) > 30:
                            snippets.append(text[:800])

                    # Extract URL
                    link = result.select_one("a.result__url, .result__a")
                    if link:
                        href = link.get("href", "")
                        if href.startswith("http") and href not in urls:
                            urls.append(href)

                logger.info(f"[ResearchAgent] DDG HTML: {len(snippets)} snippets")
                break

            except Exception as e:
                if attempt == 0:
                    logger.warning(f"[ResearchAgent] DDG HTML attempt 1 failed: {e}")
                    await asyncio.sleep(2)
                else:
                    logger.error(f"[ResearchAgent] DDG HTML failed: {e}")

        return snippets, urls

    async def _ddg_json(self, query: str) -> tuple[list[str], list[str]]:
        snippets: list[str] = []
        urls:     list[str] = []
        try:
            url = DDG_JSON_URL.format(q=quote_plus(query))
            async with httpx.AsyncClient(
                headers=HEADERS, follow_redirects=True, timeout=10
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()

            # Abstract (direct answer)
            if data.get("Abstract"):
                snippets.append(data["Abstract"])
            if data.get("AbstractURL"):
                urls.append(data["AbstractURL"])

            # Related topics
            for topic in data.get("RelatedTopics", [])[:8]:
                text = topic.get("Text", "")
                link = topic.get("FirstURL", "")
                if text and len(text) > 20:
                    snippets.append(text[:600])
                if link and link not in urls:
                    urls.append(link)

            logger.info(f"[ResearchAgent] DDG JSON: {len(snippets)} snippets")

        except Exception as e:
            logger.warning(f"[ResearchAgent] DDG JSON failed: {e}")

        return snippets, urls

    async def _fetch_page(self, url: str) -> Optional[str]:
        """Fetch and extract text from a single URL (best-effort)."""
        try:
            async with httpx.AsyncClient(
                headers=HEADERS, follow_redirects=True, timeout=PAGE_FETCH_TIMEOUT
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                ct = resp.headers.get("content-type", "")
                if "html" not in ct:
                    return None

            soup = BeautifulSoup(resp.text, "lxml")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            text = soup.get_text(separator=" ", strip=True)
            text = re.sub(r"\s{3,}", "  ", text)
            return text[:PAGE_FETCH_LIMIT] if text else None

        except Exception as e:
            logger.debug(f"[ResearchAgent] Page fetch failed ({url}): {e}")
            return None
