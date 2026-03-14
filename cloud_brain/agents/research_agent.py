"""
AetherAI — Research Agent  (Stage 5 — fully patched)

All fixes applied
─────────────────
FIX A  _fetch_page() NameError: response variable was stored as `r` inside
       the `async with` block but referenced as `resp` in BeautifulSoup call
       outside it. Changed to `r.text` throughout and restructured so all
       reads happen inside the `with` block.

FIX B  DDG HTML URL extraction: DuckDuckGo result links are redirect URLs
       like `//duckduckgo.com/l/?uddg=https%3A...` — they never start with
       "http" so the old `href.startswith("http")` guard filtered ALL of
       them out. Now decodes the `uddg=` query param to get the real URL.

Original Stage 4 features retained:
  • DuckDuckGo HTML + JSON fallback
  • Source URLs in summary
  • Snippet deduplication
  • Query cleaning / filler-phrase stripping
  • Retry on HTTP errors
  • Per-URL fetch cap
  • CancelledError pass-through
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

PAGE_FETCH_LIMIT   = 3000
PAGE_FETCH_TIMEOUT = 8.0
MAX_SNIPPETS       = 6

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


def _decode_ddg_url(href: str) -> Optional[str]:
    """
    FIX B: DDG HTML result hrefs look like:
      //duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com&rut=...
    Extract and decode the real destination URL from the `uddg=` param.
    """
    if href.startswith("http"):
        return href  # already a real URL (rare but possible)
    if not href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urlparse(href)
        qs     = parse_qs(parsed.query)
        uddg   = qs.get("uddg", [None])[0]
        if uddg:
            return unquote(uddg)
    except Exception:
        pass
    return None


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
        snippets, urls = await self._ddg_html(query)

        if len(snippets) < 2:
            logger.info("[ResearchAgent] HTML thin — trying DDG JSON API")
            json_snippets, json_urls = await self._ddg_json(query)
            snippets += json_snippets
            urls     += [u for u in json_urls if u not in urls]

        snippets = _dedup(snippets)

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
                    html_text = resp.text          # buffer before client closes

                soup = BeautifulSoup(html_text, "lxml")

                for result in soup.select(".result"):
                    body = result.select_one(".result__body, .result__snippet")
                    if body:
                        text = body.get_text(separator=" ", strip=True)
                        if len(text) > 30:
                            snippets.append(text[:800])

                    # FIX B: decode DDG redirect URL to get real destination
                    link = result.select_one("a.result__url, .result__a")
                    if link:
                        href     = link.get("href", "")
                        real_url = _decode_ddg_url(href)
                        if real_url and real_url not in urls:
                            urls.append(real_url)

                logger.info(f"[ResearchAgent] DDG HTML: {len(snippets)} snippets, {len(urls)} URLs")
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

            if data.get("Abstract"):
                snippets.append(data["Abstract"])
            if data.get("AbstractURL"):
                urls.append(data["AbstractURL"])

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
        """FIX A: all reads happen inside the `with` block; no stale `resp` reference."""
        try:
            async with httpx.AsyncClient(
                headers=HEADERS, follow_redirects=True, timeout=PAGE_FETCH_TIMEOUT
            ) as client:
                r = await client.get(url)
                r.raise_for_status()
                ct = r.headers.get("content-type", "")
                if "html" not in ct:
                    return None
                # Read text while client is still open (buffered anyway, but explicit)
                page_html = r.text

            soup = BeautifulSoup(page_html, "lxml")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            text = soup.get_text(separator=" ", strip=True)
            text = re.sub(r"\s{3,}", "  ", text)
            return text[:PAGE_FETCH_LIMIT] if text else None

        except Exception as e:
            logger.debug(f"[ResearchAgent] Page fetch failed ({url}): {e}")
            return None
