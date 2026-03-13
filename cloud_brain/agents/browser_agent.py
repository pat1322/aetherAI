"""
AetherAI — Browser Agent  (Stage 4 — hardened, patched)

Fixes applied
─────────────
FIX 7  Hacker News commands like "Go to Hacker News and summarise top stories"
       were being routed to _web_search (DuckDuckGo) rather than
       _hacker_news_top() because the keyword check only ran inside
       _run_workflow, but those commands sometimes arrived as action="search"
       (not action="workflow") from the planner. Added a HN keyword check
       directly in run() so _hacker_news_top() is always called regardless
       of which action the planner emitted.

FIX 8  YouTube playlist and search-result links were broken when the Invidious
       or Piped API returned a videoId that was actually a playlist token or
       contained extra query params. Fixed _format_yt_results() to validate
       that videoId is a proper 11-character video ID; if not, fall back to a
       YouTube search results URL so the link always works.
"""

import asyncio
import logging
import re
import time
import json as _json
from typing import Optional
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

from agents import BaseAgent

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

PAGE_TEXT_LIMIT  = 6000
MAX_HTTP_RETRIES = 2
HTTP_BACKOFF     = 1.5
INVIDIOUS_TTL    = 600

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

INVIDIOUS_INSTANCES = [
    "https://invidious.io.lol",
    "https://inv.nadeko.net",
    "https://invidious.nerdvpn.de",
    "https://yt.cdaut.de",
    "https://invidious.privacyredirect.com",
    "https://iv.melmac.space",
]

PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.adminforge.de",
    "https://pipedapi.tokhmi.xyz",
]

NOISE_TAGS = [
    "script", "style", "nav", "footer", "header", "aside",
    "noscript", "iframe", "form", "button", "svg",
    "[class*='cookie']", "[class*='gdpr']", "[class*='banner']",
    "[class*='popup']", "[id*='cookie']", "[id*='modal']",
]

# ── Hacker News keyword set (FIX 7) ───────────────────────────────────────────

HN_KEYWORDS = (
    "hacker news", "hackernews", "ycombinator",
    "news.ycombinator", "hn top", "top stories on hn",
)

# ── Playwright availability cached at import time ─────────────────────────────

def _check_playwright() -> bool:
    try:
        from pathlib import Path
        cache = Path.home() / ".cache" / "ms-playwright"
        if not cache.exists():
            return False
        return any(True for p in cache.rglob("chrome-headless-shell") if p.is_file())
    except Exception:
        return False

PLAYWRIGHT_AVAILABLE: bool = _check_playwright()

# ── Invidious instance cache ──────────────────────────────────────────────────

_invidious_cache: dict = {"url": None, "expires": 0.0}


async def _get_healthy_invidious(timeout: float = 2.5) -> Optional[str]:
    now = time.monotonic()
    if _invidious_cache["url"] and now < _invidious_cache["expires"]:
        return _invidious_cache["url"]

    async with httpx.AsyncClient(headers=HEADERS, timeout=timeout) as client:
        for base in INVIDIOUS_INSTANCES:
            try:
                r = await client.head(f"{base}/api/v1/stats")
                if r.status_code < 500:
                    _invidious_cache["url"]     = base
                    _invidious_cache["expires"] = now + INVIDIOUS_TTL
                    logger.info(f"[BrowserAgent] Healthy Invidious: {base}")
                    return base
            except Exception:
                continue

    logger.warning("[BrowserAgent] All Invidious instances unhealthy")
    return None


# ── HTTP helpers ──────────────────────────────────────────────────────────────

async def _get_with_retry(
    url: str,
    timeout: float = 15.0,
    retries: int = MAX_HTTP_RETRIES,
) -> httpx.Response:
    delay = HTTP_BACKOFF
    last_exc: Exception = RuntimeError("no attempts made")
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=timeout) as client:
        for attempt in range(retries + 1):
            try:
                r = await client.get(url)
                r.raise_for_status()
                return r
            except Exception as e:
                last_exc = e
                if attempt < retries:
                    logger.debug(f"[BrowserAgent] HTTP retry {attempt+1}: {url} — {e}")
                    await asyncio.sleep(delay)
                    delay *= 2
    raise last_exc


def _clean_soup(soup: BeautifulSoup) -> str:
    for selector in NOISE_TAGS:
        try:
            for tag in soup.select(selector):
                tag.decompose()
        except Exception:
            pass

    raw = soup.get_text(separator="\n", strip=True)
    lines = raw.splitlines()

    seen: set[str] = set()
    deduped: list[str] = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if s in seen and len(s) < 80:
            continue
        seen.add(s)
        deduped.append(s)

    text = "\n".join(deduped)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:PAGE_TEXT_LIMIT]


async def _httpx_get_page(url: str, timeout: float = 15.0) -> tuple[str, str]:
    r    = await _get_with_retry(url, timeout=timeout)
    soup = BeautifulSoup(r.text, "lxml")
    title = soup.title.string.strip() if soup.title else url
    text  = _clean_soup(soup)
    return title, text


# ── Agent ─────────────────────────────────────────────────────────────────────

class BrowserAgent(BaseAgent):
    name        = "browser_agent"
    description = "Controls a browser to navigate websites, search, and extract information"

    async def run(self, parameters: dict, task_id: str, context: str = "") -> str:
        action = parameters.get("action", "search")
        url    = parameters.get("url", "")
        query  = parameters.get("query", "") or context
        goal   = parameters.get("goal", "") or query

        logger.info(
            f"[BrowserAgent] action={action} playwright={PLAYWRIGHT_AVAILABLE} "
            f"query={query[:60]}"
        )

        # FIX 7: Intercept Hacker News regardless of whether the planner emitted
        # action="search" or action="workflow". The keyword check now lives here
        # in run() so it always fires before dispatching to _web_search.
        goal_lc = goal.lower()
        if any(k in goal_lc for k in HN_KEYWORDS):
            return await self._hacker_news_top()

        if action == "youtube":
            return await self._youtube_search(parameters.get("query") or context)
        elif action in ("scrape", "read"):
            return await self._scrape_url(url or query)
        elif action == "workflow":
            return await self._run_workflow(
                goal=parameters.get("goal") or context,
                start_url=url,
            )
        else:
            return await self._web_search(
                query=query,
                engine=parameters.get("engine", "duckduckgo"),
            )

    # ── Web search ────────────────────────────────────────────────────────────

    async def _web_search(self, query: str, engine: str = "duckduckgo") -> str:
        ddg_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"

        if PLAYWRIGHT_AVAILABLE:
            text = await self._playwright_get_text(ddg_url)
        else:
            try:
                r    = await _get_with_retry(ddg_url, timeout=15.0)
                soup = BeautifulSoup(r.text, "lxml")
                snippets: list[str] = []
                for sel in (
                    ".result__body",
                    ".result__snippet",
                    "[data-result='snippet']",
                    ".result",
                ):
                    for el in soup.select(sel)[:8]:
                        t = el.get_text(" ", strip=True)
                        if t:
                            snippets.append(t[:600])
                    if snippets:
                        break
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

    # ── Scrape ────────────────────────────────────────────────────────────────

    async def _scrape_url(self, url: str) -> str:
        if not url.startswith("http"):
            url = "https://" + url

        if "news.ycombinator.com" in url or url.rstrip("/") in (
            "https://news.ycombinator.com",
            "http://news.ycombinator.com",
        ):
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

    # ── YouTube ───────────────────────────────────────────────────────────────

    async def _youtube_search(self, query: str) -> str:
        """Search YouTube with a hard 20-second global timeout to prevent hanging."""
        try:
            return await asyncio.wait_for(self._youtube_search_inner(query), timeout=20.0)
        except asyncio.TimeoutError:
            logger.warning("[BrowserAgent] YouTube search timed out — falling back to web search")
            return await self._web_search(f"youtube {query} videos site:youtube.com")

    async def _youtube_search_inner(self, query: str) -> str:
        result = await self._youtube_via_invidious(query)
        if result:
            return result

        result = await self._youtube_via_piped(query)
        if result:
            return result

        logger.warning("[BrowserAgent] All YouTube APIs failed — using DDG fallback")
        return await self._web_search(f"youtube {query}")

    async def _youtube_via_invidious(self, query: str) -> Optional[str]:
        base = await _get_healthy_invidious()
        if not base:
            return None
        try:
            url  = f"{base}/api/v1/search?q={quote_plus(query)}&type=video&page=1"
            r    = await _get_with_retry(url, timeout=10.0)
            data = r.json()
            return self._format_yt_results(query, data, source="Invidious") if data else None
        except Exception as e:
            logger.warning(f"[BrowserAgent] Invidious search failed: {e}")
            _invidious_cache["url"] = None
            return None

    async def _youtube_via_piped(self, query: str) -> Optional[str]:
        for base in PIPED_INSTANCES:
            try:
                url  = f"{base}/search?q={quote_plus(query)}&filter=videos"
                r    = await _get_with_retry(url, timeout=5.0)
                items = r.json().get("items", [])
                if not items:
                    continue
                normalised = [
                    {
                        "title":         item.get("title", ""),
                        "videoId":       item.get("url", "").replace("/watch?v=", ""),
                        "author":        item.get("uploaderName", ""),
                        "viewCount":     item.get("views", 0),
                        "lengthSeconds": item.get("duration", 0),
                    }
                    for item in items[:8]
                ]
                result = self._format_yt_results(query, normalised, source="Piped")
                if result:
                    return result
            except Exception as e:
                logger.debug(f"[BrowserAgent] Piped {base} failed: {e}")
        return None

    @staticmethod
    def _format_yt_results(query: str, items: list, source: str = "") -> Optional[str]:
        results = []
        for item in items[:8]:
            title  = item.get("title", "")
            vid_id = item.get("videoId", "").strip()
            author = item.get("author", "")
            views  = int(item.get("viewCount", 0) or 0)
            dur    = int(item.get("lengthSeconds", 0) or 0)
            mins, secs = divmod(dur, 60)

            if not title or not vid_id:
                continue

            # FIX 8: Validate that vid_id is a proper YouTube video ID (11 chars,
            # alphanumeric + - _). Playlist tokens, full URLs, or truncated IDs
            # produce broken watch links. Fall back to a search results URL instead
            # so the link always opens something useful.
            if re.match(r'^[\w\-]{11}$', vid_id):
                link = f"https://youtube.com/watch?v={vid_id}"
            else:
                # vid_id is malformed — use a search URL that always works
                link = f"https://youtube.com/results?search_query={quote_plus(title)}"

            results.append(
                f"• **{title}**\n"
                f"  {author} | {views:,} views | {mins}:{secs:02d}\n"
                f"  {link}"
            )
        if not results:
            return None
        tag = f" _(via {source})_" if source else ""
        return f"🎬 **YouTube: {query}**{tag}\n\n" + "\n\n".join(results)

    # ── Hacker News ───────────────────────────────────────────────────────────

    async def _hacker_news_top(self, n: int = 10) -> str:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://hacker-news.firebaseio.com/v0/topstories.json"
                )
                resp.raise_for_status()
                ids = resp.json()[:n]

                async def fetch_story(sid: int) -> Optional[dict]:
                    try:
                        r = await client.get(
                            f"https://hacker-news.firebaseio.com/v0/item/{sid}.json"
                        )
                        return r.json()
                    except Exception:
                        return None

                stories = await asyncio.gather(*[fetch_story(sid) for sid in ids])

            lines = []
            for i, s in enumerate(stories, 1):
                if not s:
                    continue
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

    # ── Workflow ──────────────────────────────────────────────────────────────

    async def _run_workflow(self, goal: str, start_url: str = "") -> str:
        goal_lc = goal.lower()

        # FIX 7 (secondary guard): also check here for robustness
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

    # ── Playwright ────────────────────────────────────────────────────────────

    async def _playwright_get_text(self, url: str, wait_ms: int = 1500) -> str:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page    = await browser.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_timeout(wait_ms)
                html = await page.content()
                soup = BeautifulSoup(html, "lxml")
                return _clean_soup(soup)
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
                                    "Return ONLY valid JSON. No markdown fences."
                                ),
                                user_message=(
                                    f"Goal: {goal}\nPage: {title}\n"
                                    f"Content:\n{text[:2000]}\n\n"
                                    f"Step {step_num}/7. Pick ONE action:\n"
                                    '{"action":"goto","url":"..."}\n'
                                    '{"action":"click_text","text":"..."}\n'
                                    '{"action":"fill","selector":"...","value":"..."}\n'
                                    '{"action":"key","key":"Enter"}\n'
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
                        await page.goto(cmd["url"], wait_until="domcontentloaded", timeout=15000)
                    elif act == "click_text":
                        try:
                            await page.get_by_text(cmd["text"], exact=False).first.click(timeout=5000)
                        except Exception:
                            pass
                    elif act == "fill":
                        try:
                            await page.fill(cmd["selector"], cmd.get("value", ""), timeout=5000)
                        except Exception:
                            pass
                    elif act == "key":
                        await page.keyboard.press(cmd.get("key", "Enter"))
                    else:
                        results.append(text[:2000])
                        break

                if not results:
                    html = await page.content()
                    results.append(_clean_soup(BeautifulSoup(html, "lxml")))

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
