"""
AetherAI — Browser Agent (Stage 4)
Full Playwright implementation. Runs headlessly on the cloud brain server.

Capabilities:
  - Navigate to any URL
  - Click elements (by text, selector, or coordinates)
  - Fill forms and search boxes
  - Extract page text / scrape content
  - Take screenshots of pages
  - Handle multi-step browser workflows (login, search, read, etc.)

Usage in commands:
  "Search YouTube for lofi music"
  "Go to Wikipedia and find the history of the Philippines"
  "Open Hacker News and summarize the top 5 stories"
  "Search Google for the latest Python news"
"""

import asyncio
import base64
import logging
import re
from typing import Optional

from agents import BaseAgent
from utils.qwen_client import QwenClient

logger = logging.getLogger(__name__)

# Max characters of page text to send to Qwen for summarization
PAGE_TEXT_LIMIT = 6000


def _check_playwright():
    try:
        from playwright.async_api import async_playwright
        return True
    except ImportError:
        return False


class BrowserAgent(BaseAgent):
    name = "browser_agent"
    description = "Controls a browser to navigate websites, search, and extract information"

    async def run(self, parameters: dict, task_id: str, context: str = "") -> str:
        if not _check_playwright():
            return (
                "⚠️ Playwright not installed on the server.\n"
                "Run: pip install playwright && playwright install chromium"
            )

        action = parameters.get("action", "navigate")
        url    = parameters.get("url", "")
        query  = parameters.get("query", "") or context

        # Auto-detect action from parameters
        if not action or action == "navigate":
            if parameters.get("search") or "search" in str(parameters.get("goal", "")).lower():
                action = "search"
            elif parameters.get("scrape") or parameters.get("extract"):
                action = "scrape"

        logger.info(f"[BrowserAgent] action={action} url={url} query={query[:60]}")

        if action == "search":
            return await self._web_search(
                query=parameters.get("query") or parameters.get("search") or context,
                engine=parameters.get("engine", "google"),
            )
        elif action == "scrape" or action == "read":
            return await self._scrape_url(url or query)
        elif action == "youtube":
            return await self._youtube_search(
                query=parameters.get("query") or context
            )
        elif action == "workflow":
            return await self._run_workflow(
                goal=parameters.get("goal") or context,
                start_url=url,
            )
        else:
            # Default: navigate + extract
            if url:
                return await self._scrape_url(url)
            else:
                return await self._web_search(query=query)

    # ── Google / Bing search ──────────────────────────────────────────────────

    async def _web_search(self, query: str, engine: str = "google") -> str:
        from playwright.async_api import async_playwright

        search_urls = {
            "google": f"https://www.google.com/search?q={_encode(query)}",
            "bing":   f"https://www.bing.com/search?q={_encode(query)}",
            "duckduckgo": f"https://duckduckgo.com/?q={_encode(query)}",
        }
        url = search_urls.get(engine, search_urls["google"])

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page    = await browser.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(1500)

                # Extract search result snippets
                text = await _extract_text(page)
                logger.info(f"[BrowserAgent] Search page extracted {len(text)} chars")

                summary = await self.qwen.summarize(
                    content=text[:PAGE_TEXT_LIMIT],
                    context=f"Search query: {query}\nSummarize the key results clearly."
                )
                return f"🔍 Search results for '{query}':\n\n{summary}"

            except Exception as e:
                logger.error(f"[BrowserAgent] Search error: {e}")
                return f"⚠️ Browser search failed: {e}"
            finally:
                await browser.close()

    # ── Scrape a URL ──────────────────────────────────────────────────────────

    async def _scrape_url(self, url: str) -> str:
        from playwright.async_api import async_playwright

        if not url.startswith("http"):
            url = "https://" + url

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page    = await browser.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_timeout(1500)

                title = await page.title()
                text  = await _extract_text(page)
                logger.info(f"[BrowserAgent] Scraped '{title}' — {len(text)} chars")

                summary = await self.qwen.summarize(
                    content=text[:PAGE_TEXT_LIMIT],
                    context=f"Page title: {title}\nURL: {url}\nSummarize the main content."
                )
                return f"🌐 **{title}**\n{url}\n\n{summary}"

            except Exception as e:
                logger.error(f"[BrowserAgent] Scrape error: {e}")
                return f"⚠️ Could not load {url}: {e}"
            finally:
                await browser.close()

    # ── YouTube search ────────────────────────────────────────────────────────

    async def _youtube_search(self, query: str) -> str:
        from playwright.async_api import async_playwright

        url = f"https://www.youtube.com/results?search_query={_encode(query)}"

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page    = await browser.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(2000)

                # Extract video titles and links
                results = []
                items = await page.query_selector_all("ytd-video-renderer")
                for item in items[:8]:
                    try:
                        title_el = await item.query_selector("#video-title")
                        if title_el:
                            title = (await title_el.inner_text()).strip()
                            href  = await title_el.get_attribute("href")
                            if title and href:
                                results.append(f"• {title}\n  https://youtube.com{href}")
                    except Exception:
                        continue

                if results:
                    return f"🎬 YouTube results for '{query}':\n\n" + "\n\n".join(results)
                else:
                    # Fallback to text extraction
                    text = await _extract_text(page)
                    return f"🎬 YouTube search for '{query}':\n\n{text[:1500]}"

            except Exception as e:
                return f"⚠️ YouTube search failed: {e}"
            finally:
                await browser.close()

    # ── Multi-step workflow ───────────────────────────────────────────────────

    async def _run_workflow(self, goal: str, start_url: str = "") -> str:
        """
        AI-driven browser workflow. Qwen decides each action based on a screenshot.
        Similar to the vision loop but runs headlessly in the browser.
        """
        from playwright.async_api import async_playwright

        if not start_url:
            # Ask Qwen for the best starting URL
            url_prompt = (
                f"What is the best URL to start with to accomplish this goal: {goal}\n"
                "Return ONLY the URL, nothing else."
            )
            start_url = await self.qwen.chat(
                system_prompt="Return ONLY a URL, no explanation.",
                user_message=url_prompt,
                temperature=0.1,
            )
            start_url = start_url.strip().split()[0]

        if not start_url.startswith("http"):
            start_url = "https://" + start_url

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page    = await browser.new_page()
            results = []

            try:
                await page.goto(start_url, wait_until="domcontentloaded", timeout=20000)

                for step in range(1, 8):
                    await page.wait_for_timeout(1000)
                    text = await _extract_text(page)
                    title = await page.title()

                    action_prompt = f"""
Goal: {goal}
Current page: {title} ({page.url})
Page content (truncated): {text[:2000]}

Step {step}. What is the best next action?
Return ONLY valid JSON in one of these formats:

Navigate: {{"action": "goto", "url": "https://..."}}
Click text: {{"action": "click_text", "text": "exact text to click"}}
Fill input: {{"action": "fill", "selector": "input[name='q']", "value": "search term"}}
Press key: {{"action": "key", "key": "Enter"}}
Extract & done: {{"action": "done", "extract": "what you found"}}
"""
                    raw = await self.qwen.chat(
                        system_prompt="Browser automation agent. Return ONLY valid JSON.",
                        user_message=action_prompt,
                        temperature=0.1,
                    )
                    raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

                    try:
                        import json
                        cmd = json.loads(raw)
                    except Exception:
                        logger.warning(f"[BrowserAgent] Bad JSON at step {step}: {raw[:100]}")
                        break

                    act = cmd.get("action", "")
                    logger.info(f"[BrowserAgent] Step {step}: {act}")

                    if act == "done":
                        results.append(cmd.get("extract", text[:1000]))
                        break
                    elif act == "goto":
                        await page.goto(cmd["url"], wait_until="domcontentloaded", timeout=15000)
                    elif act == "click_text":
                        try:
                            await page.get_by_text(cmd["text"], exact=False).first.click(timeout=5000)
                        except Exception as e:
                            logger.warning(f"click_text failed: {e}")
                    elif act == "fill":
                        try:
                            await page.fill(cmd["selector"], cmd.get("value", ""), timeout=5000)
                        except Exception as e:
                            logger.warning(f"fill failed: {e}")
                    elif act == "key":
                        await page.keyboard.press(cmd.get("key", "Enter"))
                    else:
                        # Fallback: extract current page
                        results.append(text[:2000])
                        break

                if not results:
                    text = await _extract_text(page)
                    results.append(text[:3000])

                combined = "\n\n".join(results)
                summary = await self.qwen.summarize(
                    content=combined,
                    context=f"Goal: {goal}\nSummarize what was found."
                )
                return f"🤖 Browser workflow result:\n\n{summary}"

            except Exception as e:
                logger.error(f"[BrowserAgent] Workflow error: {e}")
                return f"⚠️ Browser workflow failed: {e}"
            finally:
                await browser.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _encode(text: str) -> str:
    from urllib.parse import quote_plus
    return quote_plus(text)


async def _extract_text(page) -> str:
    """Extract readable text from a Playwright page, removing nav/footer noise."""
    try:
        # Remove nav, footer, ads, scripts
        await page.evaluate("""
            () => {
                const selectors = ['nav', 'footer', 'header', 'script', 'style',
                                   '[role="banner"]', '[role="navigation"]',
                                   '.advertisement', '.ad', '#cookie-banner'];
                selectors.forEach(sel => {
                    document.querySelectorAll(sel).forEach(el => el.remove());
                });
            }
        """)
    except Exception:
        pass

    try:
        text = await page.inner_text("body")
        # Collapse whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()
    except Exception:
        return ""
