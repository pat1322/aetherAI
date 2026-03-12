"""
AetherAI — Browser Agent (Stage 4)
Playwright with reliable httpx fallbacks.

KEY FIXES:
  - Search: DuckDuckGo HTML (allows bots, no CAPTCHA)
  - YouTube: Invidious public API (real titles, views, links)
  - Hacker News: official Firebase API
  - Wikipedia/URLs: direct httpx scraping (works fine)
  - Google: NOT used for httpx — it blocks bots
"""

import asyncio
import logging
import re
import json as _json

import httpx
from bs4 import BeautifulSoup

from agents import BaseAgent

logger = logging.getLogger(__name__)

PAGE_TEXT_LIMIT = 6000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

INVIDIOUS_INSTANCES = [
    "https://invidious.io.lol",
    "https://inv.nadeko.net",
    "https://invidious.nerdvpn.de",
    "https://yt.cdaut.de",
]


def _encode(text: str) -> str:
    from urllib.parse import quote_plus
    return quote_plus(text)


def _playwright_available() -> bool:
    try:
        from pathlib import Path
        cache = Path.home() / ".cache" / "ms-playwright"
        if not cache.exists():
            return False
        for p in cache.rglob("chrome-headless-shell"):
            if p.is_file():
                return True
        return False
    except Exception:
        return False


async def _httpx_get_text(url: str, timeout: float = 15.0) -> tuple:
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=timeout) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        title = soup.title.string.strip() if soup.title else url
        text  = soup.get_text(separator="\n", strip=True)
        text  = re.sub(r"\n{3,}", "\n\n", text)
        return title, text[:PAGE_TEXT_LIMIT]


class BrowserAgent(BaseAgent):
    name = "browser_agent"
    description = "Controls a browser to navigate websites, search, and extract information"

    async def run(self, parameters: dict, task_id: str, context: str = "") -> str:
        action = parameters.get("action", "search")
        url    = parameters.get("url", "")
        query  = parameters.get("query", "") or context

        use_playwright = _playwright_available()
        logger.info(f"[BrowserAgent] action={action} playwright={use_playwright} query={query[:60]}")

        if action == "youtube":
            return await self._youtube_search(parameters.get("query") or context)
        elif action in ("scrape", "read"):
            return await self._scrape_url(url or query, use_playwright)
        elif action == "workflow":
            return await self._run_workflow(
                goal=parameters.get("goal") or context,
                start_url=url,
                use_playwright=use_playwright,
            )
        else:
            return await self._web_search(
                query=query,
                engine=parameters.get("engine", "duckduckgo"),
                use_playwright=use_playwright,
            )

    async def _web_search(self, query: str, engine: str = "duckduckgo",
                          use_playwright: bool = False) -> str:
        ddg_url = f"https://html.duckduckgo.com/html/?q={_encode(query)}"
        if use_playwright:
            text = await self._playwright_get_text(ddg_url)
        else:
            try:
                _, text = await _httpx_get_text(ddg_url)
            except Exception as e:
                logger.error(f"[BrowserAgent] DDG failed: {e}")
                answer = await self.qwen.answer(query)
                return f"🔍 **{query}**\n\n{answer}"

        summary = await self.qwen.summarize(
            content=text[:PAGE_TEXT_LIMIT],
            context=f"Search query: {query}\nSummarize the key findings clearly."
        )
        return f"🔍 **{query}**\n\n{summary}"

    async def _scrape_url(self, url: str, use_playwright: bool = False) -> str:
        if not url.startswith("http"):
            url = "https://" + url
        if "news.ycombinator.com" in url or url.rstrip("/") in (
            "https://news.ycombinator.com", "http://news.ycombinator.com"
        ):
            return await self._hacker_news_top()
        if use_playwright:
            text  = await self._playwright_get_text(url)
            title = url
        else:
            try:
                title, text = await _httpx_get_text(url)
            except Exception as e:
                return f"⚠️ Could not load {url}: {e}"
        summary = await self.qwen.summarize(
            content=text[:PAGE_TEXT_LIMIT],
            context=f"URL: {url}\nSummarize the main content clearly."
        )
        return f"🌐 **{title}**\n{url}\n\n{summary}"

    async def _youtube_search(self, query: str) -> str:
        for instance in INVIDIOUS_INSTANCES:
            try:
                url = f"{instance}/api/v1/search?q={_encode(query)}&type=video&page=1"
                async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=10.0) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    data = resp.json()
                if not data:
                    continue
                results = []
                for item in data[:8]:
                    title    = item.get("title", "")
                    vid_id   = item.get("videoId", "")
                    author   = item.get("author", "")
                    views    = item.get("viewCount", 0)
                    duration = item.get("lengthSeconds", 0)
                    mins, secs = divmod(duration, 60)
                    if title and vid_id:
                        results.append(
                            f"• **{title}**\n"
                            f"  {author} | {views:,} views | {mins}:{secs:02d}\n"
                            f"  https://youtube.com/watch?v={vid_id}"
                        )
                if results:
                    return f"🎬 **YouTube: {query}**\n\n" + "\n\n".join(results)
            except Exception as e:
                logger.warning(f"[BrowserAgent] Invidious {instance} failed: {e}")
                continue

        # All Invidious instances failed
        return await self._web_search(f"youtube {query}", engine="duckduckgo")

    async def _hacker_news_top(self, n: int = 10) -> str:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get("https://hacker-news.firebaseio.com/v0/topstories.json")
                resp.raise_for_status()
                ids = resp.json()[:n]

                async def fetch_story(sid):
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
                url   = s.get("url", f"https://news.ycombinator.com/item?id={s.get('id','')}")
                score = s.get("score", 0)
                by    = s.get("by", "?")
                comms = s.get("descendants", 0)
                lines.append(
                    f"{i}. **{title}**\n"
                    f"   ▲ {score} pts · by {by} · {comms} comments\n"
                    f"   {url}"
                )
            if lines:
                return "📰 **Hacker News — Top Stories**\n\n" + "\n\n".join(lines)
            return "⚠️ Could not retrieve Hacker News stories."
        except Exception as e:
            return f"⚠️ Hacker News API failed: {e}"

    async def _run_workflow(self, goal: str, start_url: str = "",
                            use_playwright: bool = False) -> str:
        goal_lc = goal.lower()
        if "hacker news" in goal_lc or "hackernews" in goal_lc or "ycombinator" in goal_lc:
            return await self._hacker_news_top()
        if "youtube" in goal_lc:
            query = re.sub(r".*(search|find|look up|on youtube)\s*", "", goal_lc).strip() or goal
            return await self._youtube_search(query)

        if not start_url:
            start_url = (await self.qwen.chat(
                system_prompt="Return ONLY a URL, nothing else.",
                user_message=f"Best URL to start for: {goal}",
                temperature=0.1,
            )).strip().split()[0]

        if not start_url.startswith("http"):
            start_url = "https://" + start_url

        if use_playwright:
            return await self._playwright_workflow(goal, start_url)
        return await self._scrape_url(start_url, use_playwright=False)

    async def _playwright_get_text(self, url: str, wait_ms: int = 1500) -> str:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page    = await browser.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_timeout(wait_ms)
                text = await page.inner_text("body")
                return re.sub(r"\n{3,}", "\n\n", text).strip()
            finally:
                await browser.close()

    async def _playwright_workflow(self, goal: str, start_url: str) -> str:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page    = await browser.new_page()
            results = []
            try:
                await page.goto(start_url, wait_until="domcontentloaded", timeout=20000)
                for step in range(1, 8):
                    await page.wait_for_timeout(1000)
                    text  = await self._playwright_get_text(page.url)
                    title = await page.title()
                    raw = await self.qwen.chat(
                        system_prompt="Browser automation. Return ONLY valid JSON.",
                        user_message=(
                            f"Goal: {goal}\nPage: {title}\nContent: {text[:2000]}\n"
                            f"Step {step}. JSON only:\n"
                            '{"action":"goto","url":"..."} or\n'
                            '{"action":"click_text","text":"..."} or\n'
                            '{"action":"fill","selector":"...","value":"..."} or\n'
                            '{"action":"key","key":"Enter"} or\n'
                            '{"action":"done","extract":"summary"}'
                        ),
                        temperature=0.1,
                    )
                    raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
                    try:
                        cmd = _json.loads(raw)
                    except Exception:
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
                    text = await self._playwright_get_text(page.url)
                    results.append(text[:3000])
                summary = await self.qwen.summarize(
                    content="\n\n".join(results),
                    context=f"Goal: {goal}\nSummarize what was found."
                )
                return f"🤖 **Browser result:**\n\n{summary}"
            except Exception as e:
                return f"⚠️ Browser workflow failed: {e}"
            finally:
                await browser.close()
