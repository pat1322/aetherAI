"""
AetherAI — News Agent
Primary:  GNews API (100 req/day free, requires GNEWS_API_KEY in env)
Fallback: Hacker News Firebase API (no key, always free)

Capabilities:
  • Top headlines by topic (tech, business, sports, health, science, entertainment)
  • Country-specific news (defaults to Philippines)
  • Search news by keyword
  • Morning briefing (curated summary)
  • Hacker News top stories (no key needed)

Trigger examples:
  "give me today's tech news"
  "what's the latest news"
  "morning briefing"
  "news about AI today"
  "top news in the Philippines"
  "business news"
  "science news this week"
"""

import asyncio
import logging
import os
from typing import Optional

import httpx
from agents import BaseAgent

logger = logging.getLogger(__name__)

GNEWS_BASE = "https://gnews.io/api/v4"
HN_BASE    = "https://hacker-news.firebaseio.com/v0"

CATEGORY_MAP = {
    "tech":          "technology", "technology": "technology",
    "business":      "business",   "finance":    "business",
    "sports":        "sports",     "sport":      "sports",
    "health":        "health",     "medical":    "health",
    "science":       "science",    "research":   "science",
    "entertainment": "entertainment", "celebrity": "entertainment",
    "world":         "world",      "general":    "general",
    "nation":        "nation",     "philippines": "nation",
    "ph":            "nation",
}


class NewsAgent(BaseAgent):
    name        = "news_agent"
    description = "Latest news headlines, briefings, and topic searches via GNews and Hacker News"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.gnews_key = os.getenv("GNEWS_API_KEY", "")

    async def run(self, parameters: dict, task_id: str, context: str = "") -> Optional[str]:
        try:
            return await self._run(parameters, task_id, context)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[NewsAgent] Error: {e}", exc_info=True)
            return f"⚠️ NewsAgent error: {e}"

    async def _run(self, parameters: dict, task_id: str, context: str) -> Optional[str]:
        query    = parameters.get("query") or context or ""
        ql       = query.lower()

        # Detect intent
        if any(k in ql for k in ["hacker news", "hackernews", "hn"]):
            return await self._hacker_news()

        if any(k in ql for k in ["morning briefing", "daily briefing", "news briefing", "brief me"]):
            return await self._morning_briefing()

        category = self._extract_category(ql)
        search   = self._extract_search_term(ql)

        if self.gnews_key:
            return await self._gnews(category=category, search=search, query=query)
        else:
            logger.info("[NewsAgent] No GNEWS_API_KEY — using Hacker News fallback")
            return await self._hacker_news()

    # ── GNews ─────────────────────────────────────────────────────────────────

    async def _gnews(self, category: str = "general",
                     search: str = "", query: str = "") -> str:
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                if search:
                    params = {
                        "q":       search,
                        "lang":    "en",
                        "max":     10,
                        "apikey":  self.gnews_key,
                    }
                    r = await client.get(f"{GNEWS_BASE}/search", params=params)
                else:
                    params = {
                        "category": category or "general",
                        "lang":     "en",
                        "country":  "ph",
                        "max":      10,
                        "apikey":   self.gnews_key,
                    }
                    r = await client.get(f"{GNEWS_BASE}/top-headlines", params=params)

                if r.status_code == 403:
                    logger.warning("[NewsAgent] GNews API key invalid or exhausted")
                    return await self._hacker_news()

                r.raise_for_status()
                data     = r.json()
                articles = data.get("articles", [])

        except Exception as e:
            logger.warning(f"[NewsAgent] GNews failed: {e} — falling back to HN")
            return await self._hacker_news()

        if not articles:
            return await self._hacker_news()

        label = search or category.title() or "Today"
        lines = [f"## 📰 {label} News\n"]
        for i, a in enumerate(articles[:8], 1):
            title   = a.get("title", "")
            desc    = a.get("description", "")
            url     = a.get("url", "")
            source  = a.get("source", {}).get("name", "")
            pubdate = a.get("publishedAt", "")[:10]

            lines.append(
                f"{i}. **{title}**\n"
                f"   {desc[:120]}{'...' if len(desc or '') > 120 else ''}\n"
                f"   _📌 {source} · {pubdate}_ — {url}"
            )

        # Ask Qwen for a brief synthesis
        headlines_text = "\n".join(
            f"- {a.get('title','')}. {a.get('description','')}"
            for a in articles[:8]
        )
        synthesis = await self.qwen.chat(
            system_prompt=(
                "You are a news analyst. Given these headlines, write a 2-3 sentence "
                "synthesis of the key themes and what matters most. Be concise and direct."
            ),
            user_message=f"News topic: {label}\nHeadlines:\n{headlines_text}",
            temperature=0.4,
        )

        lines.insert(1, f"**Summary:** {synthesis}\n")
        lines.append("\n_Powered by GNews.io_")
        return "\n\n".join(lines)

    # ── Morning briefing ──────────────────────────────────────────────────────

    async def _morning_briefing(self) -> str:
        results = []

        # Fetch tech + world news in parallel
        if self.gnews_key:
            tech_task  = self._fetch_gnews_raw("technology")
            world_task = self._fetch_gnews_raw("world")
            tech_arts, world_arts = await asyncio.gather(tech_task, world_task)
            all_articles = tech_arts[:4] + world_arts[:4]
        else:
            all_articles = []

        hn_task = self._fetch_hn_raw(8)

        if all_articles:
            headlines = "\n".join(
                f"- [{a.get('source',{}).get('name','')}] {a.get('title','')}. "
                f"{a.get('description','')[:100]}"
                for a in all_articles
            )
        else:
            hn_stories = await hn_task
            headlines  = "\n".join(
                f"- [Hacker News] {s.get('title','')}" for s in hn_stories
            )

        briefing = await self.qwen.chat(
            system_prompt=(
                "You are a professional news briefer. Given these headlines, write a "
                "concise morning briefing in 3-5 paragraphs. Cover the most important "
                "stories, group related topics, and end with a brief outlook. "
                "Write in a clear, professional tone."
            ),
            user_message=f"Today's headlines:\n{headlines}",
            temperature=0.5,
        )

        return f"## ☀️ Morning Briefing\n\n{briefing}\n\n_Powered by GNews.io + Hacker News_"

    # ── Hacker News ───────────────────────────────────────────────────────────

    async def _hacker_news(self, n: int = 10) -> str:
        stories = await self._fetch_hn_raw(n)
        if not stories:
            return "⚠️ Could not fetch Hacker News stories."

        lines = ["## 📰 Hacker News — Top Stories\n"]
        for i, s in enumerate(stories, 1):
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
        return "\n\n".join(lines)

    # ── Raw fetchers ──────────────────────────────────────────────────────────

    async def _fetch_gnews_raw(self, category: str) -> list:
        if not self.gnews_key:
            return []
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{GNEWS_BASE}/top-headlines", params={
                    "category": category, "lang": "en",
                    "max": 5, "apikey": self.gnews_key,
                })
                r.raise_for_status()
                return r.json().get("articles", [])
        except Exception as e:
            logger.warning(f"[NewsAgent] GNews raw fetch failed ({category}): {e}")
            return []

    async def _fetch_hn_raw(self, n: int = 10) -> list:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{HN_BASE}/topstories.json")
                ids  = resp.json()[:n]
                stories = await asyncio.gather(*[
                    self._fetch_hn_story(client, sid) for sid in ids
                ])
            return [s for s in stories if s]
        except Exception as e:
            logger.warning(f"[NewsAgent] HN fetch failed: {e}")
            return []

    async def _fetch_hn_story(self, client: httpx.AsyncClient, sid: int) -> Optional[dict]:
        try:
            r = await client.get(f"{HN_BASE}/item/{sid}.json")
            return r.json()
        except Exception:
            return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _extract_category(self, query_lc: str) -> str:
        for keyword, category in CATEGORY_MAP.items():
            if keyword in query_lc:
                return category
        return "general"

    def _extract_search_term(self, query_lc: str) -> str:
        import re
        patterns = [
            r"news\s+about\s+(.+?)(?:\s+today|\s+this\s+week|$)",
            r"latest\s+(.+?)\s+news",
            r"(.+?)\s+news\s+today",
        ]
        for pat in patterns:
            m = re.search(pat, query_lc)
            if m:
                term = m.group(1).strip()
                # Skip if it's just a category word
                if term not in CATEGORY_MAP:
                    return term
        return ""
