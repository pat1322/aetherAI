"""
AetherAI — Browser Agent (Stage 4)
Full Playwright implementation with httpx fallback.

If Playwright's Chromium binary is not installed, automatically falls back
to httpx + BeautifulSoup for scraping and DuckDuckGo for search.
This means the agent always works — Playwright just gives better results
on JS-heavy pages.
"""

import asyncio
import logging
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from agents import BaseAgent
from utils.qwen_client import QwenClient

logger = logging.getLogger(__name__)

PAGE_TEXT_LIMIT = 6000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def _playwright_available() -> bool:
    """Check if Playwright AND the Chromium binary are both present."""
    try:
        from playwright.sync_api import sync_playwright
        import subprocess, sys
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
            capture_output=True, text=True, timeout=5
        )
        # If chromium is already installed, no download lines appear
        # Simpler: just try to find the executable
        from pathlib import Path
        import os
        cache = Path.home() / ".cache" / "ms-playwright"
        if not cache.exists():
            return False
        # Look for any chromium/chrome-headless-shell binary
        for p in cache.rglob("chrome-headless-shell"):
            if p.is_file():
                return True
        for p in cache.rglob("chrome"):
            if p.is_file() and "chromium" in str(p):
                return True
        return False
    except Exception:
        return False


def _encode(text: str) -> str:
    from urllib.parse import quote_plus
    return quote_plus(text)


async def _httpx_get_text(url: str, timeout: float = 15.0) -> tuple[str, str]:
    """Fetch a URL with httpx and return (title, text)."""
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=timeout) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        # Remove noise
        for tag in soup(["script", "style", "nav", "footer", "header"]):
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
        mode = "Playwright" if use_playwright else "httpx (fallback)"
        logger.info(f"[BrowserAgent] action={action} mode={mode} query={query[:60]}")

        if action == "youtube":
            return await self._youtube_search(
                query=parameters.get("query") or context,
                use_playwright=use_playwright,
            )
        elif action in ("scrape", "read"):
            return await self._scrape_url(url or query, use_playwright)
        elif action == "workflow":
            return await self._run_workflow(
                goal=parameters.get("goal") or context,
                start_url=url,
                use_playwright=use_playwright,
            )
        else:
            # Default: search
            return await self._web_search(
                query=query,
                engine=parameters.get("engine", "google"),
                use_playwright=use_playwright,
            )

    # ── Google / Bing search ──────────────────────────────────────────────────

    async def _web_search(self, query: str, engine: str = "google",
                          use_playwright: bool = False) -> str:
        search_urls = {
            "google":     f"https://www.google.com/search?q={_encode(query)}",
            "bing":       f"https://www.bing.com/search?q={_encode(query)}",
            "duckduckgo": f"https://html.duckduckgo.com/html/?q={_encode(query)}",
        }
        url = search_urls.get(engine, search_urls["google"])

        if use_playwright:
            text = await self._playwright_get_text(url)
        else:
            try:
                _, text = await _httpx_get_text(url)
            except Exception as e:
                # Ultimate fallback: DuckDuckGo HTML
                try:
                    ddg_url = f"https://html.duckduckgo.com/html/?q={_encode(query)}"
                    _, text = await _httpx_get_text(ddg_url)
                except Exception as e2:
                    return f"⚠️ Search failed: {e2}"

        summary = await self.qwen.summarize(
            content=text[:PAGE_TEXT_LIMIT],
            context=f"Search query: {query}\nSummarize the key results clearly and concisely."
        )
        return f"🔍 **{query}**\n\n{summary}"

    # ── Scrape a URL ──────────────────────────────────────────────────────────

    async def _scrape_url(self, url: str, use_playwright: bool = False) -> str:
        if not url.startswith("http"):
            url = "https://" + url

        if use_playwright:
            title = url
            text  = await self._playwright_get_text(url)
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

    # ── YouTube search ────────────────────────────────────────────────────────

    async def _youtube_search(self, query: str, use_playwright: bool = False) -> str:
        url = f"https://www.youtube.com/results?search_query={_encode(query)}"

        if use_playwright:
            # Playwright can render the JS-heavy YouTube page properly
            text = await self._playwright_get_text(url, wait_ms=2000)
            summary = await self.qwen.summarize(
                content=text[:PAGE_TEXT_LIMIT],
                context=f"YouTube search results for: {query}\n"
                        "List the video titles and channels found. Be specific."
            )
            return f"🎬 **YouTube: {query}**\n\n{summary}"
        else:
            # Fallback: use YouTube's RSS / search suggestion API + Google search
            try:
                # Search Google for YouTube results — much more reliable than scraping YT directly
                google_url = f"https://www.google.com/search?q=site:youtube.com+{_encode(query)}"
                _, text = await _httpx_get_text(google_url)
                summary = await self.qwen.summarize(
                    content=text[:PAGE_TEXT_LIMIT],
                    context=f"YouTube search results for '{query}' via Google. "
                            "List the video titles and YouTube links found."
                )
                return f"🎬 **YouTube: {query}**\n\n{summary}"
            except Exception as e:
                # Last resort: ask Qwen from training knowledge
                answer = await self.qwen.answer(
                    f"What are some popular YouTube videos or channels about: {query}?"
                )
                return f"🎬 **YouTube: {query}**\n\n{answer}\n\n_(Note: live YouTube search unavailable — install Playwright for real results)_"

    # ── Multi-step workflow ───────────────────────────────────────────────────

    async def _run_workflow(self, goal: str, start_url: str = "",
                            use_playwright: bool = False) -> str:
        if not start_url:
            url_prompt = (
                f"What is the best URL to start with to accomplish: {goal}\n"
                "Return ONLY the full URL."
            )
            start_url = (await self.qwen.chat(
                system_prompt="Return ONLY a URL, nothing else.",
                user_message=url_prompt,
                temperature=0.1,
            )).strip().split()[0]

        if not start_url.startswith("http"):
            start_url = "https://" + start_url

        if use_playwright:
            return await self._playwright_workflow(goal, start_url)
        else:
            # Fallback: just scrape the start URL and summarize
            return await self._scrape_url(start_url, use_playwright=False)

    # ── Playwright helpers ────────────────────────────────────────────────────

    async def _playwright_get_text(self, url: str, wait_ms: int = 1500) -> str:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page    = await browser.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_timeout(wait_ms)
                try:
                    await page.evaluate("""
                        () => {
                            ['nav','footer','header','script','style',
                             '[role="banner"]','[role="navigation"]',
                             '.advertisement','.ad','#cookie-banner'
                            ].forEach(sel =>
                                document.querySelectorAll(sel).forEach(el => el.remove())
                            );
                        }
                    """)
                except Exception:
                    pass
                text = await page.inner_text("body")
                text = re.sub(r"\n{3,}", "\n\n", text)
                text = re.sub(r"[ \t]{2,}", " ", text)
                return text.strip()
            finally:
                await browser.close()

    async def _playwright_workflow(self, goal: str, start_url: str) -> str:
        import json as _json
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

                    action_prompt = f"""
Goal: {goal}
Current page: {title} ({page.url})
Content: {text[:2000]}

Step {step}. Return ONLY valid JSON:
Navigate: {{"action":"goto","url":"https://..."}}
Click:     {{"action":"click_text","text":"exact text"}}
Fill:      {{"action":"fill","selector":"input[name=q]","value":"term"}}
Key:       {{"action":"key","key":"Enter"}}
Done:      {{"action":"done","extract":"summary of what you found"}}
"""
                    raw = await self.qwen.chat(
                        system_prompt="Browser automation. Return ONLY valid JSON.",
                        user_message=action_prompt,
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
                logger.error(f"[BrowserAgent] Workflow error: {e}")
                return f"⚠️ Browser workflow failed: {e}"
            finally:
                await browser.close()
