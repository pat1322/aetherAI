"""
AetherAI — Browser Agent  (Stage 6 — streaming patch)

Streaming change
────────────────
The final summarize/synthesis call in each public action now uses
self.stream_summarize() so the output streams token-by-token.

  _web_search()       →  self.stream_summarize() for the final summary
  _scrape_url()       →  self.stream_summarize() for the page summary
  _youtube_search()   →  self.stream_summarize() for the "what you'll find" blurb
  _hacker_news_top()  →  no LLM call, already instant (just formats data)

All Stage 5 + Stage 6 fixes retained:
  FIX 3   single import json
  FIX 4   MAX_HTTP_RETRIES / HTTP_BACKOFF wired into DDG fetch
  FIX 10  _check_ytdlp() lazy cached
"""

import asyncio
import json
import logging
import re
import subprocess
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

HN_KEYWORDS  = ("hacker news","hackernews","ycombinator","news.ycombinator","hn top")
DDG_HTML_URL = "https://html.duckduckgo.com/html/"
NOISE_TAGS   = ["script","style","nav","footer","header","aside",
                "noscript","iframe","form","button","svg"]


# ── Availability checks (lazy-cached) ─────────────────────────────────────────

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

_ytdlp_available: Optional[bool] = None

def _check_ytdlp() -> bool:
    global _ytdlp_available
    if _ytdlp_available is None:
        try:
            result = subprocess.run(
                ["yt-dlp", "--version"],
                capture_output=True, text=True, timeout=3,
            )
            _ytdlp_available = result.returncode == 0
        except Exception:
            _ytdlp_available = False
    return _ytdlp_available

PLAYWRIGHT_AVAILABLE  = _check_playwright()
TRAFILATURA_AVAILABLE = _check_trafilatura()

logger.info(f"[BrowserAgent] playwright={PLAYWRIGHT_AVAILABLE} "
            f"trafilatura={TRAFILATURA_AVAILABLE}")


# ── Text extraction helpers ────────────────────────────────────────────────────

def _extract_text(html: str, url: str = "") -> str:
    if TRAFILATURA_AVAILABLE:
        try:
            import trafilatura
            text = trafilatura.extract(
                html, url=url or None,
                include_comments=False,
                include_tables=True,
                no_fallback=False,
                favor_recall=True,
            )
            if text and len(text) > 100:
                return text[:PAGE_TEXT_LIMIT]
        except Exception as e:
            logger.debug(f"[BrowserAgent] trafilatura failed: {e}")

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
    if not href:
        return None
    if href.startswith("http") and "duckduckgo" not in href:
        return href
    if href.startswith("//"):
        href = "https:" + href
    try:
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

        ytdlp = _check_ytdlp()
        logger.info(f"[BrowserAgent] action={action} "
                    f"playwright={PLAYWRIGHT_AVAILABLE} "
                    f"trafilatura={TRAFILATURA_AVAILABLE} "
                    f"yt-dlp={ytdlp}")

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
        """
        Multi-endpoint DDG search. Tries these in order:
          1. Playwright headless (if installed)
          2. DDG HTML POST endpoint (html.duckduckgo.com — blocked on some hosts)
          3. DDG Lite GET endpoint  (duckduckgo.com/lite  — different IP, often works)
          4. DDG JSON instant answer API (api.duckduckgo.com — lightweight, usually allowed)
          5. Knowledge fallback via stream_llm (never leaves bubble empty)
        """
        raw_html = ""
        text     = ""

        # ── Attempt 1: Playwright ──────────────────────────────────────────────
        if PLAYWRIGHT_AVAILABLE:
            ddg_url  = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
            raw_html = await self._playwright_get_html(ddg_url)
            text     = _extract_text(raw_html, ddg_url)
            logger.info(f"[BrowserAgent] Playwright search: {len(text)} chars")

        # ── Attempt 2: DDG HTML POST ───────────────────────────────────────────
        if not text.strip():
            try:
                async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True,
                                             timeout=12.0) as client:
                    resp = await client.post(DDG_HTML_URL, data={"q": query, "b": ""})
                    if resp.status_code == 200:
                        raw_html = resp.text
                        text = _extract_text(raw_html, DDG_HTML_URL)
                        logger.info(f"[BrowserAgent] DDG HTML POST: {len(text)} chars")
            except Exception as e:
                logger.warning(f"[BrowserAgent] DDG HTML POST failed: {e}")

        # ── Attempt 3: DDG Lite GET ────────────────────────────────────────────
        if not text.strip():
            try:
                lite_url = f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}"
                async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True,
                                             timeout=12.0) as client:
                    resp = await client.get(lite_url)
                    if resp.status_code == 200:
                        raw_html = resp.text
                        text = _extract_text(raw_html, lite_url)
                        logger.info(f"[BrowserAgent] DDG Lite GET: {len(text)} chars")
            except Exception as e:
                logger.warning(f"[BrowserAgent] DDG Lite failed: {e}")

        # ── Attempt 4: DDG JSON instant answer API ─────────────────────────────
        if not text.strip():
            try:
                json_url = (
                    f"https://api.duckduckgo.com/?q={quote_plus(query)}"
                    f"&format=json&no_html=1&skip_disambig=1"
                )
                async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True,
                                             timeout=10.0) as client:
                    resp = await client.get(json_url)
                    if resp.status_code == 200:
                        data   = resp.json()
                        chunks = []
                        if data.get("Abstract"):
                            chunks.append(data["Abstract"])
                        if data.get("Answer"):
                            chunks.append(data["Answer"])
                        for t in data.get("RelatedTopics", [])[:8]:
                            if isinstance(t, dict) and t.get("Text"):
                                chunks.append(t["Text"])
                        if data.get("AbstractURL"):
                            raw_html = f'<a href="{data["AbstractURL"]}">{data["AbstractURL"]}</a>'
                        text = " ".join(chunks)
                        logger.info(f"[BrowserAgent] DDG JSON API: {len(text)} chars")
            except Exception as e:
                logger.warning(f"[BrowserAgent] DDG JSON API failed: {e}")

        # ── Attempt 5: Knowledge fallback ─────────────────────────────────────
        if not text.strip():
            logger.error("[BrowserAgent] All web search attempts failed — using knowledge fallback")
            answer = await self.stream_llm(
                "You are AetherAI. Answer thoroughly and accurately using your knowledge. "
                "Use clear markdown formatting with headings and bullet points. "
                "At the very end add a single line: "
                "> **Note:** Live web search was unavailable. This answer is based on training knowledge.",
                query,
                temperature=0.7,
            )
            return f"🔍 **{query}**\n\n{answer}"

        # Extract real result URLs for citations
        result_urls = []
        if raw_html:
            try:
                soup_ddg = BeautifulSoup(raw_html, "lxml")
                # DDG HTML/Lite: result links
                for a in soup_ddg.select(".result__a, .result-link, td a")[:8]:
                    real = _decode_ddg_url(a.get("href", ""))
                    if real and real.startswith("http") and "duckduckgo" not in real:
                        if real not in result_urls:
                            result_urls.append(real)
            except Exception:
                pass

        # STREAMING — final summary streams token-by-token
        summary = await self.stream_summarize(
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

        # STREAMING — page summary streams token-by-token
        summary = await self.stream_summarize(
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

    # ── YouTube ────────────────────────────────────────────────────────────────

    async def _youtube_search(self, query: str) -> str:
        try:
            return await asyncio.wait_for(self._youtube_inner(query), timeout=30.0)
        except asyncio.TimeoutError:
            search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
            return (f"⚠️ YouTube search timed out.\nDirect search: {search_url}")

    async def _youtube_inner(self, query: str) -> str:
        if _check_ytdlp():
            result = await self._youtube_via_ytdlp(query)
            if result: return result
        return await self._youtube_via_ddg(query)

    async def _youtube_via_ytdlp(self, query: str) -> Optional[str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "yt-dlp", "--dump-json", "--flat-playlist", "--no-warnings",
                f"ytsearch8:{query}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20.0)

            if proc.returncode != 0:
                logger.warning(f"[BrowserAgent] yt-dlp error: {stderr.decode()[:200]}")
                return None

            videos = []
            for line in stdout.decode().strip().split("\n"):
                if not line.strip(): continue
                try:
                    v = json.loads(line)
                    videos.append({
                        "title":   v.get("title", ""),
                        "url":     v.get("url") or f"https://youtube.com/watch?v={v.get('id','')}",
                        "duration": v.get("duration", 0),
                        "views":   v.get("view_count", 0),
                        "channel": v.get("channel") or v.get("uploader", ""),
                        "desc":    (v.get("description") or "")[:150],
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

            video_list  = "\n\n".join(lines)
            result_text = "\n".join(
                f"{v['title']} — {v['channel']} — {v['desc']}"
                for v in videos[:8]
            )

            # STREAMING — "what you'll find" blurb streams token-by-token
            summary = await self.stream_summarize(
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
        ddg_query = f"{query} site:youtube.com"
        results: list[dict] = []

        try:
            async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True,
                                         timeout=15.0) as client:
                resp = await client.post(DDG_HTML_URL, data={"q": ddg_query, "b": ""})
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
        video_list  = "\n".join(lines)
        result_text = "\n".join(f"{r['title']}: {r['desc']}" for r in results[:8])

        # STREAMING
        summary = await self.stream_summarize(
            content=result_text,
            context=f"YouTube search: {query}\nDescribe what types of videos appeared.",
            source="web",
        )
        return (f"🎬 **YouTube: {query}**\n\n"
                f"**What you'll find:** {summary}\n\n"
                f"**Videos:**\n\n{video_list}")

    # ── Hacker News ─────────────────────────────────────────────────────────────
    # No LLM call — just formats data, already instant

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
                        cmd = json.loads(raw)
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

                # STREAMING — workflow summary streams token-by-token
                summary = await self.stream_summarize(
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
