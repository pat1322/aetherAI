"""
AetherAI — Research Agent  (Stage 6)

Stage 6 upgrade: trafilatura replaces BeautifulSoup for page fetching,
giving much cleaner article text with no ads/navbars/cookie banners.
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

DDG_HTML_URL    = "https://html.duckduckgo.com/html/"
DDG_JSON_URL    = "https://api.duckduckgo.com/?q={q}&format=json&no_html=1&skip_disambig=1"
PAGE_FETCH_LIMIT   = 6000
PAGE_FETCH_TIMEOUT = 10.0
MAX_SNIPPETS       = 8


def _check_trafilatura() -> bool:
    try:
        import trafilatura  # noqa
        return True
    except ImportError:
        return False

TRAFILATURA_AVAILABLE = _check_trafilatura()


def _extract_page_text(html: str, url: str = "") -> Optional[str]:
    """Use trafilatura if available, else BeautifulSoup fallback."""
    if TRAFILATURA_AVAILABLE:
        try:
            import trafilatura
            text = trafilatura.extract(
                html, url=url or None,
                include_comments=False,
                include_tables=True,
                favor_recall=True,
            )
            if text and len(text) > 100:
                return text[:PAGE_FETCH_LIMIT]
        except Exception as e:
            logger.debug(f"[ResearchAgent] trafilatura failed: {e}")

    # BeautifulSoup fallback
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script","style","nav","footer","header","aside","form"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s{3,}", "  ", text)
    return text[:PAGE_FETCH_LIMIT] if text else None


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
        qs   = parse_qs(urlparse(href).query)
        uddg = qs.get("uddg", [None])[0]
        if uddg: return unquote(uddg)
    except Exception:
        pass
    return None


class ResearchAgent(BaseAgent):
    name        = "research_agent"
    description = "Academic researcher — structured reports with real web sources and citations"

    async def run(self, parameters: dict, task_id: str, context: str = "") -> Optional[str]:
        try:
            return await self._run(parameters, task_id, context)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[ResearchAgent] Error: {e}", exc_info=True)
            return f"⚠️ ResearchAgent error: {e}"

    async def _run(self, parameters: dict, task_id: str, context: str) -> Optional[str]:
        raw_query = parameters.get("query") or "general research"
        query     = _clean_query(raw_query)
        logger.info(f"[ResearchAgent] Query: '{query}' | browser_context={bool(context and len(context)>200)}")

        # Step 2 of research pipeline — browser_agent already fetched live data
        if context and len(context) > 200:
            logger.info(f"[ResearchAgent] Synthesizing from browser context ({len(context)} chars)")
            return await self._write_brief_from_context(query, context)

        # Standalone research — do our own search
        snippets, urls = await self._search(query)
        if not snippets:
            return await self._knowledge_brief(raw_query)

        return await self._write_brief(query, snippets, urls)

    async def _write_brief_from_context(self, query: str, browser_output: str) -> str:
        """
        Synthesize a research report from browser-fetched live content.
        URLs are extracted in Python first so Qwen cannot hallucinate them.
        """
        import re

        # ── Extract REAL URLs from browser output before calling Qwen ──────────
        raw_urls = re.findall(
            r'https?://[^\s<>"\')\]]+',
            browser_output
        )
        # Clean trailing punctuation, deduplicate, keep max 8
        seen, real_urls = set(), []
        for u in raw_urls:
            u = u.rstrip(".,;:!?)\'\"")
            if u not in seen and len(u) > 10:
                # Skip common noise
                if not any(noise in u for noise in [
                    "duckduckgo.com", "google.com/search",
                    "javascript:", "data:", "example.com",
                ]):
                    seen.add(u)
                    real_urls.append(u)
            if len(real_urls) >= 8:
                break

        sources_block = (
            "\n".join(f"- {u}" for u in real_urls)
            if real_urls else
            "_No URLs were returned by the web search for this query._"
        )

        prompt = f'''You are writing a research brief about: "{query}"

The following content was retrieved LIVE from the web seconds ago.
Base your report ENTIRELY on this content.

ABSOLUTE RULES:
- NEVER invent, guess, or modify URLs
- NEVER use "as of my knowledge cutoff" or "based on my training data"
- The ## Sources section MUST contain ONLY the URLs listed in REAL SOURCES below — copy them exactly
- Use only plain markdown. Never output HTML tags like <a>, <br>, <p>

FORMAT YOUR RESPONSE:

## Overview
[2-3 sentence summary of the most important findings]

## Key Findings
- **[Topic]**: [specific fact, stat, or detail from the content]
[repeat for each major finding]

## Detailed Analysis
[2-3 paragraphs with thorough analysis of the content]

## Sources
{sources_block}

---
REAL SOURCES (copy these exactly into ## Sources, do not modify):
{sources_block}

---
LIVE WEB CONTENT:
{browser_output[:5000]}'''

        result = await self.qwen.chat(
            system_prompt=(
                "You are a precise research analyst. "
                "Write structured briefs from browser content only. "
                "NEVER invent URLs — copy them exactly from the REAL SOURCES list provided. "
                "Use only plain markdown, never HTML tags."
            ),
            user_message=prompt,
            temperature=0.3,
        )

        # ── Safety: replace any remaining <a href...> HTML in output ───────────
        result = re.sub(
            r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>([^<]*)</a>',
            r'[\2](\1)',
            result,
        )
        result = re.sub(r'<[^>]+>', '', result)

        return result

    async def _write_brief(self, query: str, snippets: list[str],
                            urls: list[str]) -> str:
        combined = "\n\n---SOURCE---\n\n".join(snippets[:MAX_SNIPPETS])

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

FORMAT:

## Overview
[2-3 sentence summary]

## Key Findings
- **[Finding]**: [Specific detail with numbers/facts]
[Add as many as the sources support]

## Detailed Analysis
[2-3 paragraphs — be thorough and specific]

## Sources
[Leave blank — appended separately]

---
LIVE WEB CONTENT:
{combined}"""

        brief = await self.qwen.chat(
            system_prompt=(
                "You are a precise academic research assistant with live web access. "
                "Write structured research briefs based strictly on provided content. "
                "Never use knowledge-cutoff disclaimers. Be thorough and specific."
            ),
            user_message=prompt,
            temperature=0.3,
        )

        if urls:
            source_lines = "\n".join(
                f"{i+1}. {u}" for i, u in enumerate(urls[:6])
            )
            if "## Sources" in brief:
                brief = re.sub(r"## Sources.*$",
                               f"## Sources\n{source_lines}",
                               brief, flags=re.DOTALL)
            else:
                brief += f"\n\n## Sources\n{source_lines}"

        return brief

    async def _knowledge_brief(self, query: str) -> str:
        return await self.qwen.answer(
            question=query,
            context=(
                "Web search returned no results. Answer from training knowledge. "
                "Use Overview/Key Findings/Analysis structure. "
                "Note at the end this is training knowledge, not live data, "
                "and suggest where to check for current info."
            ),
        )

    async def _search(self, query: str) -> tuple[list[str], list[str]]:
        snippets, urls = await self._ddg_html(query)

        if len(snippets) < 2:
            j_snip, j_urls = await self._ddg_json(query)
            snippets += j_snip
            urls     += [u for u in j_urls if u not in urls]

        snippets = _dedup(snippets)

        # Fetch full page text from top 2 URLs using trafilatura
        if urls:
            fetch_results = await asyncio.gather(
                *[self._fetch_page(u) for u in urls[:2]],
                return_exceptions=True,
            )
            for text in fetch_results:
                if isinstance(text, str) and text:
                    snippets.insert(0, text)

        snippets = _dedup(snippets)
        logger.info(f"[ResearchAgent] {len(snippets)} snippets, {len(urls)} URLs")
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
                    if resp.status_code in (429,500,502,503) and attempt == 0:
                        await asyncio.sleep(2)
                        continue
                    resp.raise_for_status()
                    html_text = resp.text

                soup = BeautifulSoup(html_text, "lxml")
                for result in soup.select(".result"):
                    body = result.select_one(".result__body, .result__snippet")
                    if body:
                        text = body.get_text(separator=" ", strip=True)
                        if len(text) > 40: snippets.append(text[:1200])
                    link = result.select_one(".result__a")
                    if link:
                        real = _decode_ddg_url(link.get("href",""))
                        if real and real not in urls and real.startswith("http"):
                            urls.append(real)
                logger.info(f"[ResearchAgent] DDG: {len(snippets)} snips, {len(urls)} URLs")
                break
            except Exception as e:
                if attempt == 0:
                    logger.warning(f"[ResearchAgent] DDG attempt 1: {e}")
                    await asyncio.sleep(2)
                else:
                    logger.error(f"[ResearchAgent] DDG failed: {e}")
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
            for topic in data.get("RelatedTopics",[])[:10]:
                text = topic.get("Text","")
                link = topic.get("FirstURL","")
                if text and len(text) > 20: snippets.append(text[:800])
                if link and link not in urls: urls.append(link)
        except Exception as e:
            logger.warning(f"[ResearchAgent] DDG JSON failed: {e}")
        return snippets, urls

    async def _fetch_page(self, url: str) -> Optional[str]:
        """Fetch page using trafilatura for clean article extraction."""
        try:
            async with httpx.AsyncClient(
                headers=HEADERS, follow_redirects=True, timeout=PAGE_FETCH_TIMEOUT
            ) as client:
                r = await client.get(url)
                r.raise_for_status()
                if "html" not in r.headers.get("content-type",""):
                    return None
                text = _extract_page_text(r.text, url)
                if text:
                    logger.info(f"[ResearchAgent] Fetched {url[:60]} ({len(text)} chars)")
                return text
        except Exception as e:
            logger.debug(f"[ResearchAgent] Fetch failed {url}: {e}")
            return None
