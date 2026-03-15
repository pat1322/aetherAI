"""
AetherAI — Browser Agent  (Stage 6)

Stage 6 upgrades:
  • trafilatura replaces BeautifulSoup for web scraping — much cleaner text extraction
  • yt-dlp replaces DDG YouTube search — real YouTube metadata, no API needed
  • source="web" on all summarize() calls — prevents "knowledge cutoff" responses
  • Increased PAGE_TEXT_LIMIT to 10000 for richer context
"""

import asyncio
import json as _json
import logging
import re
import subprocess
import json
from typing import Optional
from urllib.parse import quote_plus, urlparse, parse_qs, unquote

import httpx
from bs4 import BeautifulSoup

from agents import BaseAgent

logger = logging.getLogger(__name__)

PAGE_TEXT_LIMIT  = 10000
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

HN_KEYWORDS    = ("hacker news","hackernews","ycombinator","news.ycombinator","hn top")
DDG_HTML_URL   = "https://html.duckduckgo.com/html/"
NOISE_TAGS     = ["script","style","nav","footer","header","aside",
                   "noscript","iframe","form","button","svg"]


# ── Availability checks ────────────────────────────────────────────────────────

def _check_playwright() -> bool:
    try:
        from pathlib import Path
        cache = Path.home() / ".cache" / "ms-playwright"
        if not cache.exists(): return False
        return any(True for p in cache.rglob("chrome-headless-shell") if p.is_file())
    except Exception:
        return False

def _check_trafilatura() -> bool:
    try:
        import trafilatura  # noqa
        return True
    except ImportError:
        return False

def _check_ytdlp() -> bool:
    try:
        result = subprocess.run(["yt-dlp", "--version"],
                                capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False

PLAYWRIGHT_AVAILABLE  = _check_playwright()
TRAFILATURA_AVAILABLE = _check_trafilatura()
YTDLP_AVAILABLE       = _check_ytdlp()

logger.info(f"[BrowserAgent] playwright={PLAYWRIGHT_AVAILABLE} "
            f"trafilatura={TRAFILATURA_AVAILABLE} yt-dlp={YTDLP_AVAILABLE}")


# ── Text extraction helpers ────────────────────────────────────────────────────

def _extract_text(html: str, url: str = "") -> str:
    """
    Use trafilatura if available (far better quality), else fall back to BeautifulSoup.
    """
    if TRAFILATURA_AVAILABLE:
        try:
            import trafilatura
            text = trafilatura.extract(
                html,
                url=url or None,
                include_comments=False,
                include_tables=True,
                no_fallback=False,
                favor_recall=True,
            )
            if text and len(text) > 100:
                return text[:PAGE_TEXT_LIMIT]
        except Exception as e:
            logger.debug(f"[BrowserAgent] trafilatura failed: {e}")

    # Fallback: BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(NOISE_TAGS): tag.decompose()
    raw   = soup.get_text(separator="\n", strip=True)
    lines = raw.splitlines()
    seen: set[str] = set()
    out:  list[str] = []
    for line in lines:
        s = line.strip()
        if not s: continue
        if s in seen and len(s) < 80: continue
        seen.add(s)
        out.append(s)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:PAGE_TEXT_LIMIT]


async def _fetch_and_extract(url: str, timeout: float = 15.0) -> tuple[str, str]:
    """Fetch URL and extract clean text. Returns (title, text)."""
    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True,
                                     timeout=timeout) as client:
            r = await client.get(url)
            r.raise_for_status()
            html = r.text

        soup  = BeautifulSoup(html, "lxml")
        title = soup.title.string.strip() if soup.title else url
        text  = _extract_text(html, url)
        return title, text
    except Exception as e:
        logger.warning(f"[BrowserAgent] Fetch failed {url}: {e}")
        return url, ""


def _decode_ddg_url(href: str):
    """Decode a DuckDuckGo redirect URL to get the real destination URL."""
    if not href:
        return None
    if href.startswith("http") and "duckduckgo" not in href:
        return href
    if href.startswith("//"):
        href = "https:" + href
    try:
        from urllib.parse import urlparse, parse_qs, unquote
        qs   = parse_qs(urlparse(href).query)
        uddg = qs.get("uddg", [None])[0]
        if uddg:
            return unquote(uddg)
    except Exception:
        pass
    return None


class BrowserAgent(BaseAgent):
    name        = "browser_agent"
    description = "Real-time web: scrape URLs, YouTube, web search, Hacker News"

    async def run(self, parameters: dict, task_id: str, context: str = "") -> str:
        action = parameters.get("action", "search")
        url    = parameters.get("url", "")
        query  = parameters.get("query", "") or context
        goal   = parameters.get("goal", "") or query

        logger.info(f"[BrowserAgent] action={action} "
                    f"playwright={PLAYWRIGHT_AVAILABLE} "
                    f"trafilatura={TRAFILATURA_AVAILABLE} "
                    f"yt-dlp={YTDLP_AVAILABLE}")

        goal_lc = goal.lower()
        if any(k in goal_lc for k in HN_KEYWORDS):
            return await self._hacker_news_top()

        if action == "youtube":
            return await self._youtube_search(parameters.get("query") or context)
        elif action in ("scrape", "read"):
            return await self._scrape_url(url or query)
        elif action == "workflow":
            return await self._run_workflow(goal=goal, start_url=url)
        else:
            return await self._web_search(query=query,
                                          engine=parameters.get("engine", "google"))

    # ── Web search ─────────────────────────────────────────────────────────────

    async def _web_search(self, query: str, engine: str = "google") -> str:
        ddg_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"

        raw_html = ""
        if PLAYWRIGHT_AVAILABLE:
            raw_html = await self._playwright_get_html(ddg_url)
            text = _extract_text(raw_html, ddg_url)
        else:
            try:
                async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True,
                                             timeout=15.0) as client:
                    resp = await client.post(DDG_HTML_URL, data={"q": query, "b": ""})
                    raw_html = resp.text
                    text = _extract_text(raw_html, DDG_HTML_URL)
            except Exception as e:
                logger.error(f"[BrowserAgent] DDG failed: {e}")
                return f"🔍 **{query}**\n\n{await self.qwen.answer(query)}"

        if not text.strip():
            return f"🔍 **{query}**\n\n{await self.qwen.answer(query)}"

        # Extract real result URLs from DDG HTML for research_agent citations
        result_urls = []
        if raw_html:
            try:
                soup_ddg = BeautifulSoup(raw_html, "lxml")
                for a in soup_ddg.select(".result__a")[:8]:
                    real = _decode_ddg_url(a.get("href", ""))
                    if real and real.startswith("http") and "duckduckgo" not in real:
                        if real not in result_urls:
                            result_urls.append(real)
            except Exception:
                pass

        summary = await self.qwen.summarize(
            content=text[:PAGE_TEXT_LIMIT],
            context=(
                f"Web search for: {query}\n"
                f"Summarize the search results thoroughly. "
                f"Include all specific facts, figures, and important details found."
            ),
            source="web",
        )

        url_block = ""
        if result_urls:
            url_lines = "\n".join(f"- {u}" for u in result_urls[:6])
            url_block = f"\n\n**SEARCH_SOURCES:**\n{url_lines}"

        return f"🔍 **{query}**\n\n{summary}{url_block}"

    # ── Scrape URL ─────────────────────────────────────────────────────────────

    async def _scrape_url(self, url: str) -> str:
        if not url.startswith("http"): url = "https://" + url
        if "news.ycombinator.com" in url:
            return await self._hacker_news_top()

        if PLAYWRIGHT_AVAILABLE:
            html  = await self._playwright_get_html(url)
            title = url
            text  = _extract_text(html, url)
        else:
            title, text = await _fetch_and_extract(url)

        if not text.strip():
            return f"⚠️ No readable content found at {url}"

        summary = await self.qwen.summarize(
            content=text[:PAGE_TEXT_LIMIT],
            context=(
                f"URL: {url}\n"
                f"Summarize the content of this page in detail. "
                f"Include all key sections, facts, data, and important information. "
                f"Be comprehensive."
            ),
            source="web",
        )
        return f"🌐 **{title}**\n{url}\n\n{summary}"

    # ── YouTube (yt-dlp preferred, DDG fallback) ───────────────────────────────

    async def _youtube_search(self, query: str) -> str:
        try:
            return await asyncio.wait_for(self._youtube_inner(query), timeout=30.0)
        except asyncio.TimeoutError:
            search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
            return (f"⚠️ YouTube search timed out.\nDirect search: {search_url}")

    async def _youtube_inner(self, query: str) -> str:
        if YTDLP_AVAILABLE:
            result = await self._youtube_via_ytdlp(query)
            if result: return result

        return await self._youtube_via_ddg(query)

    async def _youtube_via_ytdlp(self, query: str) -> Optional[str]:
        """Use yt-dlp to get real YouTube video metadata."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "yt-dlp",
                "--dump-json",
                "--flat-playlist",
                "--no-warnings",
                f"ytsearch8:{query}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=20.0
            )

            if proc.returncode != 0:
                logger.warning(f"[BrowserAgent] yt-dlp error: {stderr.decode()[:200]}")
                return None

            videos = []
            for line in stdout.decode().strip().split("\n"):
                if not line.strip(): continue
                try:
                    v = json.loads(line)
                    videos.append({
                        "title":    v.get("title", ""),
                        "url":      v.get("url") or f"https://youtube.com/watch?v={v.get('id','')}",
                        "duration": v.get("duration", 0),
                        "views":    v.get("view_count", 0),
                        "channel":  v.get("channel") or v.get("uploader", ""),
                        "desc":     (v.get("description") or "")[:150],
                    })
                except json.JSONDecodeError:
                    continue

            if not videos:
                return None

            lines = []
            for i, v in enumerate(videos[:8], 1):
                dur_str = ""
                if v["duration"]:
                    mins, secs = divmod(int(v["duration"]), 60)
                    dur_str = f" · {mins}:{secs:02d}"
                views_str = f" · {v['views']:,} views" if v["views"] else ""
                lines.append(
                    f"{i}. **{v['title']}**\n"
                    f"   📺 {v['channel']}{dur_str}{views_str}\n"
                    f"   {v['url']}"
                    + (f"\n   _{v['desc']}_" if v['desc'] else "")
                )

            video_list = "\n\n".join(lines)

            # Summarize the results
            result_text = "\n".join(
                f"{v['title']} — {v['channel']} — {v['desc']}"
                for v in videos[:8]
            )
            summary = await self.qwen.summarize(
                content=result_text,
                context=(
                    f"YouTube search results for: {query}\n"
                    f"Write 2-3 sentences describing what types of videos appeared, "
                    f"what channels or topics are covered."
                ),
                source="web",
            )

            return (
                f"🎬 **YouTube: {query}**\n\n"
                f"**What you'll find:** {summary}\n\n"
                f"**Videos:**\n\n{video_list}\n\n"
                f"_Data via yt-dlp_"
            )

        except asyncio.TimeoutError:
            logger.warning("[BrowserAgent] yt-dlp timed out")
            return None
        except Exception as e:
            logger.warning(f"[BrowserAgent] yt-dlp failed: {e}")
            return None

    async def _youtube_via_ddg(self, query: str) -> str:
        """Fallback: DDG search with site:youtube.com."""
        ddg_query = f"{query} site:youtube.com"
        results: list[dict] = []

        try:
            async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True,
                                         timeout=15.0) as client:
                resp = await client.post(DDG_HTML_URL,
                                         data={"q": ddg_query, "b": ""})
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "lxml")

                for result in soup.select(".result")[:15]:
                    title_el = result.select_one(".result__a")
                    desc_el  = result.select_one(".result__snippet")
                    if not title_el: continue
                    title    = title_el.get_text(strip=True)
                    href     = title_el.get("href", "")
                    desc     = desc_el.get_text(strip=True) if desc_el else ""
                    real_url = href
                    if href.startswith("//"): href = "https:" + href
                    try:
                        qs   = parse_qs(urlparse(href).query)
                        uddg = qs.get("uddg", [None])[0]
                        if uddg: real_url = unquote(uddg)
                    except Exception:
                        pass
                    if "youtube.com/watch" in real_url or "youtu.be/" in real_url:
                        results.append({"title":title,"url":real_url,"desc":desc[:150]})
        except Exception as e:
            logger.error(f"[BrowserAgent] YouTube DDG failed: {e}")

        if not results:
            search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
            return (f"🎬 **YouTube: {query}**\n\nCould not retrieve results.\n"
                    f"Direct search: {search_url}")

        lines = []
        for i, r in enumerate(results[:8], 1):
            desc_str = f" — {r['desc'][:80]}" if r['desc'] else ""
            lines.append(f"{i}. **{r['title']}**{desc_str}\n   🔗 {r['url']}")
        video_list = "\n".join(lines)
        result_text = "\n".join(f"{r['title']}: {r['desc']}" for r in results[:8])
        summary = await self.qwen.summarize(
            content=result_text,
            context=f"YouTube search: {query}\nDescribe what types of videos appeared.",
            source="web",
        )
        return (f"🎬 **YouTube: {query}**\n\n"
                f"**What you'll find:** {summary}\n\n"
                f"**Videos:**\n\n{video_list}")

    # ── Hacker News ─────────────────────────────────────────────────────────────

    async def _hacker_news_top(self, n: int = 10) -> str:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://hacker-news.firebaseio.com/v0/topstories.json")
                resp.raise_for_status()
                ids = resp.json()[:n]
                stories = await asyncio.gather(*[
                    self._hn_story(client, sid) for sid in ids
                ])
            lines = []
            for i, s in enumerate(stories, 1):
                if not s: continue
                title = s.get("title","")
                url   = s.get("url") or f"https://news.ycombinator.com/item?id={s.get('id','')}"
                score = s.get("score",0)
                by    = s.get("by","?")
                comms = s.get("descendants",0)
                lines.append(
                    f"{i}. **{title}** — ▲{score} pts · by {by} · {comms} comments\n"
                    f"   🔗 {url}"
                )
            return ("📰 **Hacker News — Top Stories**\n\n" + "\n".join(lines)
                    if lines else "⚠️ Could not retrieve Hacker News stories.")
        except Exception as e:
            return f"⚠️ Hacker News API failed: {e}"

    async def _hn_story(self, client: httpx.AsyncClient, sid: int) -> Optional[dict]:
        try:
            r = await client.get(
                f"https://hacker-news.firebaseio.com/v0/item/{sid}.json")
            return r.json()
        except Exception:
            return None

    # ── Workflow ─────────────────────────────────────────────────────────────────

    async def _run_workflow(self, goal: str, start_url: str = "") -> str:
        goal_lc = goal.lower()
        if any(k in goal_lc for k in HN_KEYWORDS):
            return await self._hacker_news_top()
        if "youtube" in goal_lc:
            q = re.sub(r".*(search|find|on youtube)\s*", "", goal_lc).strip() or goal
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

    # ── Playwright helpers ────────────────────────────────────────────────────────

    async def _playwright_get_html(self, url: str, wait_ms: int = 1500) -> str:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page    = await browser.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_timeout(wait_ms)
                return await page.content()
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
                    text  = _extract_text(html, await page.url)
                    title = await page.title()
                    try:
                        raw = await asyncio.wait_for(
                            self.qwen.chat(
                                system_prompt="Browser automation agent. Return ONLY valid JSON.",
                                user_message=(
                                    f"Goal: {goal}\nPage: {title}\n"
                                    f"Content:\n{text[:2000]}\n\n"
                                    f"Step {step_num}/7. Pick ONE:\n"
                                    '{"action":"goto","url":"..."}\n'
                                    '{"action":"done","extract":"what you found"}'
                                ),
                                temperature=0.1,
                            ),
                            timeout=20.0,
                        )
                    except asyncio.TimeoutError:
                        results.append(text[:2000])
                        break
                    raw = re.sub(r"```(?:json)?","",raw).strip().rstrip("`").strip()
                    try:
                        cmd = _json.loads(raw)
                    except Exception:
                        results.append(text[:2000])
                        break
                    act = cmd.get("action","")
                    if act == "done":
                        results.append(cmd.get("extract", text[:1000]))
                        break
                    elif act == "goto":
                        await page.goto(cmd["url"],wait_until="domcontentloaded",timeout=15000)
                    else:
                        results.append(text[:2000])
                        break

                if not results:
                    results.append(_extract_text(await page.content()))

                summary = await self.qwen.summarize(
                    content="\n\n".join(results),
                    context=f"Goal: {goal}\nSummarize everything found in detail.",
                    source="web",
                )
                return f"🤖 **Browser result:**\n\n{summary}"
            except Exception as e:
                logger.error(f"[BrowserAgent] Playwright workflow error: {e}")
                return f"⚠️ Browser workflow failed: {e}"
            finally:
                await browser.close()
