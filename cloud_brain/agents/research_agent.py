"""
AetherAI — Research Agent  (Stage 5 — patch 4)

Identity: Academic-style researcher. Triggered ONLY by explicit "research" keyword.
Produces structured reports with real sources and numbered URL citations.

Core fix: The Qwen summarization prompt now EXPLICITLY forbids training-data
language ("as of my knowledge cutoff", "I cannot access real-time data").
It receives raw web content and must use it — not its training.
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

PAGE_FETCH_LIMIT   = 5000
PAGE_FETCH_TIMEOUT = 10.0
MAX_SNIPPETS       = 8


def _clean_query(query: str) -> str:
    return re.sub(
        r"^\s*(research|do research on|find research on|find studies on|"
        r"do some research on|investigate)\s+",
        "", query, flags=re.IGNORECASE
    ).strip()


def _dedup(snippets: list[str]) -> list[str]:
    seen, out = set(), []
    for s in snippets:
        key = hashlib.md5(s[:120].encode()).hexdigest()
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


def _decode_ddg_url(href: str) -> Optional[str]:
    if not href: return None
    if href.startswith("http") and "duckduckgo" not in href: return href
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
    description = "Academic researcher — produces structured reports with real web sources and citations"

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
        logger.info(f"[ResearchAgent] Research query: '{query}'")

        snippets, urls = await self._search(query)
        logger.info(f"[ResearchAgent] Got {len(snippets)} snippets, {len(urls)} URLs")

        if not snippets:
            logger.warning("[ResearchAgent] No web results")
            return await self._knowledge_brief(raw_query)

        return await self._write_brief(query, snippets, urls)

    # ── Write structured brief ────────────────────────────────────────────────

    async def _write_brief(self, query: str, snippets: list[str],
                            urls: list[str]) -> str:
        combined = "\n\n---SOURCE---\n\n".join(snippets[:MAX_SNIPPETS])

        # This prompt is the core fix: forbid training-data phrases explicitly
        prompt = f"""You are writing a research brief about: "{query}"

The following content was retrieved LIVE from the internet right now.
Base your research brief ENTIRELY on this content.

FORBIDDEN PHRASES — never use these:
- "as of my knowledge cutoff"
- "as of my last update"  
- "I cannot access real-time data"
- "I don't have access to current information"
- "based on my training data"

If the content contains current prices, dates, specs, or news — state them directly as facts.

FORMAT your response exactly like this:

## Overview
[2-3 sentence summary of what the research found]

## Key Findings
- **[Finding 1 title]**: [Specific detail with numbers/facts from sources]
- **[Finding 2 title]**: [Specific detail]
- **[Finding 3 title]**: [Specific detail]
[Add as many findings as the sources support]

## Detailed Analysis
[2-3 paragraphs synthesizing what the sources say — be thorough and specific]

## Sources
[Leave this blank — sources will be appended separately]

---
LIVE WEB CONTENT:
{combined}"""

        brief = await self.qwen.chat(
            system_prompt=(
                "You are a precise academic research assistant. "
                "You have been given live web content fetched right now. "
                "Write structured research briefs based strictly on the provided content. "
                "Never use knowledge-cutoff disclaimers — you have current data. "
                "Be thorough, specific, and cite exact figures when present."
            ),
            user_message=prompt,
            temperature=0.3,
        )

        # Replace the blank Sources section with real URLs
        if urls:
            source_lines = "\n".join(
                f"{i+1}. {u}" for i, u in enumerate(urls[:6])
            )
            if "## Sources" in brief:
                brief = re.sub(
                    r"## Sources.*$", f"## Sources\n{source_lines}",
                    brief, flags=re.DOTALL
                )
            else:
                brief += f"\n\n## Sources\n{source_lines}"

        return brief

    async def _knowledge_brief(self, query: str) -> str:
        """Used only when all web searches fail."""
        result = await self.qwen.answer(
            question=query,
            context=(
                "Web search returned no results. Answer from your training knowledge. "
                "Structure your response with Overview, Key Findings, and Analysis sections. "
                "Note at the end that this is based on training knowledge, not live web data, "
                "and suggest specific websites the user can check for current information."
            ),
        )
        return result

    # ── Search pipeline ────────────────────────────────────────────────────────

    async def _search(self, query: str) -> tuple[list[str], list[str]]:
        """Try multiple search strategies to maximize result quality."""
        snippets, urls = await self._ddg_html(query)

        if len(snippets) < 2:
            logger.info("[ResearchAgent] DDG HTML thin, trying JSON API")
            j_snip, j_urls = await self._ddg_json(query)
            snippets += j_snip
            urls     += [u for u in j_urls if u not in urls]

        snippets = _dedup(snippets)

        # Fetch full text from top 2 URLs for richer content
        if urls:
            fetch_results = await asyncio.gather(
                *[self._fetch_page(u) for u in urls[:2]],
                return_exceptions=True,
            )
            for text in fetch_results:
                if isinstance(text, str) and text:
                    snippets.insert(0, text)  # prepend — full page content is richer

        snippets = _dedup(snippets)
        return snippets, urls

    async def _ddg_html(self, query: str) -> tuple[list[str], list[str]]:
        snippets: list[str] = []
        urls:     list[str] = []
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(
                    headers=HEADERS, follow_redirects=True, timeout=15
                ) as client:
                    resp = await client.post(
                        DDG_HTML_URL,
                        data={"q": query, "b": ""},
                        headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
                    )
                    if resp.status_code in (429, 500, 502, 503) and attempt == 0:
                        logger.warning(f"[ResearchAgent] DDG returned {resp.status_code}, retrying")
                        await asyncio.sleep(2)
                        continue
                    resp.raise_for_status()
                    html_text = resp.text

                soup = BeautifulSoup(html_text, "lxml")

                for result in soup.select(".result"):
                    # Extract snippet
                    body = result.select_one(".result__body, .result__snippet")
                    if body:
                        text = body.get_text(separator=" ", strip=True)
                        if len(text) > 40:
                            snippets.append(text[:1200])

                    # Extract URL (decode DDG redirect)
                    link = result.select_one(".result__a")
                    if link:
                        real = _decode_ddg_url(link.get("href", ""))
                        if real and real not in urls and real.startswith("http"):
                            urls.append(real)

                logger.info(
                    f"[ResearchAgent] DDG HTML: {len(snippets)} snippets, {len(urls)} URLs"
                )
                break

            except Exception as e:
                if attempt == 0:
                    logger.warning(f"[ResearchAgent] DDG HTML attempt 1 failed: {e}")
                    await asyncio.sleep(2)
                else:
                    logger.error(f"[ResearchAgent] DDG HTML completely failed: {e}")

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

            for topic in data.get("RelatedTopics", [])[:10]:
                text = topic.get("Text", "")
                link = topic.get("FirstURL", "")
                if text and len(text) > 20:
                    snippets.append(text[:800])
                if link and link not in urls:
                    urls.append(link)

            logger.info(f"[ResearchAgent] DDG JSON: {len(snippets)} snippets")
        except Exception as e:
            logger.warning(f"[ResearchAgent] DDG JSON failed: {e}")
        return snippets, urls

    async def _fetch_page(self, url: str) -> Optional[str]:
        """Fetch full page content for richer research material."""
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
            for tag in soup(["script","style","nav","footer","header","aside",
                              "form","button","iframe"]):
                tag.decompose()
            text = soup.get_text(separator=" ", strip=True)
            text = re.sub(r"\s{3,}", "  ", text)
            logger.info(f"[ResearchAgent] Fetched page: {url[:60]} ({len(text)} chars)")
            return text[:PAGE_FETCH_LIMIT] if text else None

        except Exception as e:
            logger.debug(f"[ResearchAgent] Page fetch failed {url}: {e}")
            return None
