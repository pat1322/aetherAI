"""
AetherAI — Browser Agent  (Stage 5 — patch 3)

Identity: Real-time web access, URL scraping, YouTube search, Hacker News.
NOT for general Q&A — that's chat. NOT for academic research — that's research_agent.

Fixes applied this patch
────────────────────────
FIX P7  YouTube via Invidious and Piped APIs removed entirely. Both were
        frequently returning "I can't browse the internet" because the
        Railway instance couldn't reach those external APIs reliably, and
        the fallback was returning Qwen training data as if it were web results.

        New approach: YouTube searches go through DuckDuckGo with
        site:youtube.com, which always returns real current video links
        without needing any third-party API. Results are formatted with
        proper youtube.com/watch?v= links extracted from the DDG result URLs.

FIX P8  HN keyword guard is also present in run() so it fires regardless
        of which action the planner emitted.
"""

import asyncio
import logging
import re
import json as _json
from typing import Optional
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

from agents import BaseAgent

logger = logging.getLogger(__name__)

PAGE_TEXT_LIMIT  = 6000
MAX_HTTP_RETRIES = 2
HTTP_BACKOFF     = 1.5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

NOISE_TAGS = [
    "script","style","nav","footer","header","aside",
    "noscript","iframe","form","button","svg",
    "[class*='cookie']","[class*='gdpr']","[class*='banner']",
    "[class*='popup']","[id*='cookie']","[id*='modal']",
]

HN_KEYWORDS = ("hacker news","hackernews","ycombinator","news.ycombinator","hn top")

DDG_HTML_URL = "https://html.duckduckgo.com/html/"


def _check_playwright() -> bool:
    try:
        from pathlib import Path
        cache = Path.home() / ".cache" / "ms-playwright"
        if not cache.exists(): return False
        return any(True for p in cache.rglob("chrome-headless-shell") if p.is_file())
    except Exception:
        return False

PLAYWRIGHT_AVAILABLE: bool = _check_playwright()


async def _get_with_retry(url: str, timeout: float = 15.0,
                          retries: int = MAX_HTTP_RETRIES) -> httpx.Response:
    delay = HTTP_BACKOFF
    last_exc: Exception = RuntimeError("no attempts")
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True,
                                 timeout=timeout) as client:
        for attempt in range(retries + 1):
            try:
                r = await client.get(url)
                r.raise_for_status()
                return r
            except Exception as e:
                last_exc = e
                if attempt < retries:
                    await asyncio.sleep(delay)
                    delay *= 2
    raise last_exc


def _clean_soup(soup: BeautifulSoup) -> str:
    for sel in NOISE_TAGS:
        try:
            for tag in soup.select(sel): tag.decompose()
        except Exception:
            pass
    raw   = soup.get_text(separator="\n", strip=True)
    lines = raw.splitlines()
    seen: set[str] = set()
    deduped: list[str] = []
    for line in lines:
        s = line.strip()
        if not s: continue
        if s in seen and len(s) < 80: continue
        seen.add(s)
        deduped.append(s)
    text = "\n".join(deduped)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:PAGE_TEXT_LIMIT]


async def _httpx_get_page(url: str, timeout: float = 15.0) -> tuple[str, str]:
    r    = await _get_with_retry(url, timeout=timeout)
    soup = BeautifulSoup(r.text, "lxml")
    title = soup.title.string.strip() if soup.title else url
    return title, _clean_soup(soup)


class BrowserAgent(BaseAgent):
    name        = "browser_agent"
    description = "Real-time web access: scrape URLs, YouTube search, Hacker News, web search"

    async def run(self, parameters: dict, task_id: str, context: str = "") -> str:
        action = parameters.get("action", "search")
        url    = parameters.get("url", "")
        query  = parameters.get("query", "") or context
        goal   = parameters.get("goal", "") or query

        logger.info(f"[BrowserAgent] action={action} playwright={PLAYWRIGHT_AVAILABLE} "
                    f"query={query[:60]}")

        # HN guard — fires regardless of action
        goal_lc = goal.lower()
        if any(k in goal_lc for k in HN_KEYWORDS):
            return await self._hacker_news_top()

        if action == "youtube":
            return await self._youtube_search(parameters.get("query") or context)
        elif action in ("scrape", "read"):
            return await self._scrape_url(url or query)
        elif action == "workflow":
            return await self._run_workflow(
                goal=parameters.get("goal") or context, start_url=url)
        else:
            return await self._web_search(
                query=query,
                engine=parameters.get("engine", "duckduckgo"),
            )

    # ── Web search ─────────────────────────────────────────────────────────────

    async def _web_search(self, query: str, engine: str = "duckduckgo") -> str:
        ddg_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"

        if PLAYWRIGHT_AVAILABLE:
            text = await self._playwright_get_text(ddg_url)
        else:
            try:
                async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True,
                                             timeout=15.0) as client:
                    resp = await client.post(DDG_HTML_URL, data={"q": query, "b": ""})
                    soup = BeautifulSoup(resp.text, "lxml")
                    snippets: list[str] = []
                    for sel in (".result__body",".result__snippet",
                                "[data-result='snippet']",".result"):
                        for el in soup.select(sel)[:8]:
                            t = el.get_text(" ", strip=True)
                            if t: snippets.append(t[:600])
                        if snippets: break
                    text = "\n\n".join(snippets)
            except Exception as e:
                logger.error(f"[BrowserAgent] DDG failed: {e}")
                return f"🔍 **{query}**\n\n{await self.qwen.answer(query)}"

        if not text.strip():
            return f"🔍 **{query}**\n\n{await self.qwen.answer(query)}"

        summary = await self.qwen.summarize(
            content=text[:PAGE_TEXT_LIMIT],
            context=f"Search query: {query}\nSummarize the key findings clearly.",
        )
        return f"🔍 **{query}**\n\n{summary}"

    # ── Scrape ─────────────────────────────────────────────────────────────────

    async def _scrape_url(self, url: str) -> str:
        if not url.startswith("http"): url = "https://" + url
        if "news.ycombinator.com" in url or url.rstrip("/") in (
            "https://news.ycombinator.com","http://news.ycombinator.com"):
            return await self._hacker_news_top()

        if PLAYWRIGHT_AVAILABLE:
            text  = await self._playwright_get_text(url)
            title = url
        else:
            try:
                title, text = await _httpx_get_page(url)
            except Exception as e:
                logger.error(f"[BrowserAgent] Scrape failed {url}: {e}")
                return f"⚠️ Could not load {url}: {e}"

        if not text.strip():
            return f"⚠️ No readable content found at {url}"

        summary = await self.qwen.summarize(
            content=text[:PAGE_TEXT_LIMIT],
            context=f"URL: {url}\nSummarize the main content clearly.",
        )
        return f"🌐 **{title}**\n{url}\n\n{summary}"

    # ── YouTube search (FIX P7: DDG-based, no Invidious/Piped) ────────────────

    async def _youtube_search(self, query: str) -> str:
        """
        FIX P7: Search YouTube via DuckDuckGo with site:youtube.com.
        This always returns real, current video results without relying on
        Invidious or Piped APIs that are frequently unreachable from Railway.
        """
        try:
            return await asyncio.wait_for(
                self._youtube_via_ddg(query), timeout=25.0
            )
        except asyncio.TimeoutError:
            logger.warning("[BrowserAgent] YouTube DDG search timed out")
            return f"⚠️ YouTube search timed out. Try: https://youtube.com/results?search_query={quote_plus(query)}"

    async def _youtube_via_ddg(self, query: str) -> str:
        """Search DDG for YouTube videos and extract watch links."""
        ddg_query = f"{query} site:youtube.com"
        results: list[dict] = []

        try:
            async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True,
                                         timeout=15.0) as client:
                resp = await client.post(DDG_HTML_URL,
                                         data={"q": ddg_query, "b": ""})
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "lxml")

                for result in soup.select(".result")[:12]:
                    title_el = result.select_one(".result__a")
                    link_el  = result.select_one("a.result__url, .result__a")
                    desc_el  = result.select_one(".result__snippet")

                    if not title_el: continue
                    title = title_el.get_text(strip=True)
                    href  = link_el.get("href", "") if link_el else ""

                    # Extract real URL from DDG redirect
                    from urllib.parse import urlparse, parse_qs, unquote
                    real_url = href
                    if "duckduckgo.com/l/" in href or href.startswith("//"):
                        if href.startswith("//"): href = "https:" + href
                        try:
                            qs   = parse_qs(urlparse(href).query)
                            uddg = qs.get("uddg", [None])[0]
                            if uddg: real_url = unquote(uddg)
                        except Exception:
                            pass

                    # Only keep actual YouTube watch/channel links
                    if "youtube.com/watch" in real_url or "youtu.be/" in real_url:
                        desc = desc_el.get_text(strip=True) if desc_el else ""
                        results.append({
                            "title": title,
                            "url":   real_url,
                            "desc":  desc[:120],
                        })

        except Exception as e:
            logger.error(f"[BrowserAgent] YouTube DDG search failed: {e}")

        if results:
            lines = []
            for i, r in enumerate(results[:8], 1):
                lines.append(
                    f"{i}. **{r['title']}**\n"
                    f"   {r['url']}"
                    + (f"\n   {r['desc']}" if r['desc'] else "")
                )
            return (f"🎬 **YouTube: {query}**\n\n" + "\n\n".join(lines))

        # Fallback: direct search link
        search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
        return (
            f"🎬 **YouTube: {query}**\n\n"
            f"Direct search link: {search_url}\n\n"
            f"*(DDG didn't return direct video links — click the link above to search YouTube)*"
        )

    # ── Hacker News ────────────────────────────────────────────────────────────

    async def _hacker_news_top(self, n: int = 10) -> str:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://hacker-news.firebaseio.com/v0/topstories.json")
                resp.raise_for_status()
                ids = resp.json()[:n]

                async def fetch_story(sid: int) -> Optional[dict]:
                    try:
                        r = await client.get(
                            f"https://hacker-news.firebaseio.com/v0/item/{sid}.json")
                        return r.json()
                    except Exception:
                        return None

                stories = await asyncio.gather(*[fetch_story(sid) for sid in ids])

            lines = []
            for i, s in enumerate(stories, 1):
                if not s: continue
                title = s.get("title", "No title")
                url   = s.get("url") or f"https://news.ycombinator.com/item?id={s.get('id','')}"
                score = s.get("score", 0)
                by    = s.get("by", "?")
                comms = s.get("descendants", 0)
                lines.append(
                    f"{i}. **{title}**\n"
                    f"   ▲ {score} pts · by {by} · {comms} comments\n"
                    f"   {url}"
                )
            return ("📰 **Hacker News — Top Stories**\n\n" + "\n\n".join(lines)
                    if lines else "⚠️ Could not retrieve Hacker News stories.")
        except Exception as e:
            return f"⚠️ Hacker News API failed: {e}"

    # ── Workflow ────────────────────────────────────────────────────────────────

    async def _run_workflow(self, goal: str, start_url: str = "") -> str:
        goal_lc = goal.lower()
        if any(k in goal_lc for k in HN_KEYWORDS):
            return await self._hacker_news_top()
        if "youtube" in goal_lc:
            q = re.sub(r".*(search|find|look up|on youtube)\s*", "", goal_lc).strip() or goal
            return await self._youtube_search(q)

        if not start_url:
            raw = await self.qwen.chat(
                system_prompt="Return ONLY a valid URL. Nothing else.",
                user_message=f"Best start URL for: {goal}",
                temperature=0.1,
            )
            start_url = raw.strip().split()[0]

        if not start_url.startswith("http"):
            start_url = "https://" + start_url

        if PLAYWRIGHT_AVAILABLE:
            return await self._playwright_workflow(goal, start_url)
        return await self._scrape_url(start_url)

    # ── Playwright ──────────────────────────────────────────────────────────────

    async def _playwright_get_text(self, url: str, wait_ms: int = 1500) -> str:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page    = await browser.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_timeout(wait_ms)
                html = await page.content()
                return _clean_soup(BeautifulSoup(html, "lxml"))
            finally:
                await browser.close()

    async def _playwright_workflow(self, goal: str, start_url: str) -> str:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page    = await browser.new_page()
            results: list[str] = []
            try:
                await page.goto(start_url, wait_until="domcontentloaded", timeout=20000)
                for step_num in range(1, 8):
                    await page.wait_for_timeout(1000)
                    html  = await page.content()
                    text  = _clean_soup(BeautifulSoup(html, "lxml"))
                    title = await page.title()
                    try:
                        raw = await asyncio.wait_for(
                            self.qwen.chat(
                                system_prompt=(
                                    "You are a browser automation agent. "
                                    "Return ONLY valid JSON. No markdown."
                                ),
                                user_message=(
                                    f"Goal: {goal}\nPage: {title}\n"
                                    f"Content:\n{text[:2000]}\n\n"
                                    f"Step {step_num}/7. Pick ONE action:\n"
                                    '{"action":"goto","url":"..."}\n'
                                    '{"action":"click_text","text":"..."}\n'
                                    '{"action":"done","extract":"what you found"}'
                                ),
                                temperature=0.1,
                            ),
                            timeout=20.0,
                        )
                    except asyncio.TimeoutError:
                        results.append(text[:2000])
                        break
                    raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
                    try:
                        cmd = _json.loads(raw)
                    except Exception:
                        results.append(text[:2000])
                        break
                    act = cmd.get("action", "")
                    if act == "done":
                        results.append(cmd.get("extract", text[:1000]))
                        break
                    elif act == "goto":
                        await page.goto(cmd["url"], wait_until="domcontentloaded",
                                        timeout=15000)
                    elif act == "click_text":
                        try:
                            await page.get_by_text(
                                cmd["text"], exact=False).first.click(timeout=5000)
                        except Exception:
                            pass
                    else:
                        results.append(text[:2000])
                        break

                if not results:
                    results.append(_clean_soup(BeautifulSoup(await page.content(), "lxml")))

                summary = await self.qwen.summarize(
                    content="\n\n".join(results),
                    context=f"Goal: {goal}\nSummarize what was found.",
                )
                return f"🤖 **Browser workflow result:**\n\n{summary}"
            except Exception as e:
                logger.error(f"[BrowserAgent] Playwright workflow error: {e}")
                return f"⚠️ Browser workflow failed: {e}"
            finally:
                await browser.close()
