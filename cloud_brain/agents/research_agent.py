"""
AetherAI — Research Agent  (Stage 5 — patch 3)

Identity overhaul
─────────────────
The research agent now has a clear identity: academic/thesis-style researcher.
It is ONLY triggered when the user explicitly asks to "research" something.

What it does differently from browser_agent:
  • Searches multiple sources (DDG HTML + DDG JSON API)
  • De-duplicates and ranks snippets by quality
  • Builds a structured report: Summary → Key Findings → Sources
  • Returns numbered citations with real URLs
  • Asks Qwen to produce a research-paper-style writeup

What it does NOT do:
  • It is NOT a general web search (that's browser_agent)
  • It is NOT a chat answer (that's chat mode)

FIX A  _fetch_page NameError fixed (r.text not resp.text)
FIX B  DDG URL decoding fixed (uddg= param extraction)
FIX C  Output is now structured like a research brief, not a bullet dump
"""

import asyncio
import hashlib
import logging
import re
from typing import Optional
from urllib.parse import quote_plus, urlparse, parse_qs, unquote

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

PAGE_FETCH_LIMIT   = 4000
PAGE_FETCH_TIMEOUT = 8.0
MAX_SNIPPETS       = 8

_FILLER_RE = re.compile(
    r"^\s*(tell me about|what is|what are|explain|research|find information "
    r"(on|about)|look up|search for|give me info on|do research on|"
    r"find research on|find studies on)\s+",
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


def _decode_ddg_url(href: str) -> Optional[str]:
    """Extract real destination URL from DDG redirect href."""
    if not href: return None
    if href.startswith("http"): return href
    if href.startswith("//"): href = "https:" + href
    try:
        parsed = urlparse(href)
        qs     = parse_qs(parsed.query)
        uddg   = qs.get("uddg", [None])[0]
        if uddg: return unquote(uddg)
    except Exception:
        pass
    return None


class ResearchAgent(BaseAgent):
    name        = "research_agent"
    description = "Academic-style researcher — finds real sources and produces structured research briefs"

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
        logger.info(f"[ResearchAgent] Research query: {query}")

        snippets, urls = await self._search(query)

        if not snippets:
            logger.warning("[ResearchAgent] No web results — using Qwen knowledge")
            return await self._knowledge_answer(raw_query)

        # Build structured research brief
        return await self._write_research_brief(query, snippets, urls)

    async def _write_research_brief(
        self, query: str, snippets: list[str], urls: list[str]
    ) -> str:
        """Ask Qwen to write a structured research brief from the gathered sources."""
        combined = "\n\n---\n\n".join(snippets[:MAX_SNIPPETS])

        prompt = (
            f"You are an academic research assistant. "
            f"Based ONLY on the source material below, write a structured research brief "
            f"about: {query}\n\n"
            f"Format your response as:\n"
            f"**Overview** — 2-3 sentence summary\n\n"
            f"**Key Findings**\n"
            f"- Finding 1 with specific details\n"
            f"- Finding 2 with specific details\n"
            f"- (continue for all major findings)\n\n"
            f"**Analysis** — 1-2 paragraph synthesis of what the sources say\n\n"
            f"Use specific facts, numbers, and details from the sources. "
            f"Do not add information not present in the sources.\n\n"
            f"SOURCE MATERIAL:\n{combined}"
        )

        brief = await self.qwen.chat(
            system_prompt=(
                "You are a precise academic research assistant. "
                "Write structured research briefs based strictly on provided sources. "
                "Use clear headings and specific facts."
            ),
            user_message=prompt,
            temperature=0.3,
        )

        # Append real source URLs
        if urls:
            source_lines = "\n".join(
                f"{i+1}. {u}" for i, u in enumerate(urls[:6])
            )
            brief += f"\n\n**Sources**\n{source_lines}"

        return brief

    async def _knowledge_answer(self, query: str) -> str:
        """Fallback when web search returns nothing."""
        return await self.qwen.answer(
            question=query,
            context=(
                "Answer using your training knowledge. "
                "Be honest about the limits of your knowledge cutoff. "
                "Structure your answer with clear headings."
            ),
        )

    # ── Search pipeline ────────────────────────────────────────────────────────

    async def _search(self, query: str) -> tuple[list[str], list[str]]:
        snippets, urls = await self._ddg_html(query)

        if len(snippets) < 3:
            logger.info("[ResearchAgent] HTML results thin — trying DDG JSON")
            j_snip, j_urls = await self._ddg_json(query)
            snippets += j_snip
            urls     += [u for u in j_urls if u not in urls]

        snippets = _dedup(snippets)

        # Fetch top 2 pages directly for richer content
        fetch_tasks = []
        for url in urls[:2]:
            if url not in fetch_tasks:
                fetch_tasks.append(url)

        page_texts = await asyncio.gather(
            *[self._fetch_page(u) for u in fetch_tasks],
            return_exceptions=True,
        )
        for text in page_texts:
            if isinstance(text, str) and text:
                snippets.append(text)

        snippets = _dedup(snippets)
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
                    html_text = resp.text

                soup = BeautifulSoup(html_text, "lxml")
                for result in soup.select(".result"):
                    body = result.select_one(".result__body, .result__snippet")
                    if body:
                        text = body.get_text(separator=" ", strip=True)
                        if len(text) > 40:
                            snippets.append(text[:1000])
                    link = result.select_one("a.result__url, .result__a")
                    if link:
                        real = _decode_ddg_url(link.get("href", ""))
                        if real and real not in urls:
                            urls.append(real)
                logger.info(f"[ResearchAgent] DDG HTML: {len(snippets)} snips, {len(urls)} URLs")
                break
            except Exception as e:
                if attempt == 0:
                    logger.warning(f"[ResearchAgent] DDG HTML attempt 1: {e}")
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
            if data.get("Abstract"):
                snippets.append(data["Abstract"])
            if data.get("AbstractURL"):
                urls.append(data["AbstractURL"])
            for topic in data.get("RelatedTopics", [])[:8]:
                text = topic.get("Text", "")
                link = topic.get("FirstURL", "")
                if text and len(text) > 20:
                    snippets.append(text[:800])
                if link and link not in urls:
                    urls.append(link)
            logger.info(f"[ResearchAgent] DDG JSON: {len(snippets)} snips")
        except Exception as e:
            logger.warning(f"[ResearchAgent] DDG JSON failed: {e}")
        return snippets, urls

    async def _fetch_page(self, url: str) -> Optional[str]:
        """Fetch and extract text from a page for richer source content."""
        try:
            async with httpx.AsyncClient(
                headers=HEADERS, follow_redirects=True, timeout=PAGE_FETCH_TIMEOUT
            ) as client:
                r = await client.get(url)
                r.raise_for_status()
                if "html" not in r.headers.get("content-type", ""):
                    return None
                page_html = r.text

            soup = BeautifulSoup(page_html, "lxml")
            for tag in soup(["script","style","nav","footer","header","aside"]):
                tag.decompose()
            text = soup.get_text(separator=" ", strip=True)
            text = re.sub(r"\s{3,}", "  ", text)
            return text[:PAGE_FETCH_LIMIT] if text else None
        except Exception as e:
            logger.debug(f"[ResearchAgent] Page fetch {url}: {e}")
            return None
