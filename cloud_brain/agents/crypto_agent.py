"""
AetherAI — Crypto Agent  (patch 2)

FIX 14 Added retry with 2s back-off on CoinGecko HTTP 429 (rate limit).
       Free tier is 30 req/min — rapid queries (top-10, multi-coin) were
       silently returning error strings instead of prices.
"""

import asyncio
import logging
from typing import Optional

import httpx
from agents import BaseAgent

logger = logging.getLogger(__name__)

COINGECKO_BASE  = "https://api.coingecko.com/api/v3"
RETRY_ON_429    = 2      # max retries
RETRY_DELAY_429 = 2.0    # seconds between retries

COIN_ALIASES = {
    "bitcoin": "bitcoin",       "btc": "bitcoin",
    "ethereum": "ethereum",     "eth": "ethereum",
    "solana": "solana",         "sol": "solana",
    "bnb": "binancecoin",       "binance": "binancecoin",
    "xrp": "ripple",            "ripple": "ripple",
    "cardano": "cardano",       "ada": "cardano",
    "dogecoin": "dogecoin",     "doge": "dogecoin",
    "polygon": "matic-network", "matic": "matic-network",
    "litecoin": "litecoin",     "ltc": "litecoin",
    "polkadot": "polkadot",     "dot": "polkadot",
    "avalanche": "avalanche-2", "avax": "avalanche-2",
    "chainlink": "chainlink",   "link": "chainlink",
    "tron": "tron",             "trx": "tron",
    "shiba": "shiba-inu",       "shib": "shiba-inu",
    "uniswap": "uniswap",       "uni": "uniswap",
    "ton": "the-open-network",
}

TOP_COINS = [
    "bitcoin", "ethereum", "tether", "binancecoin", "solana",
    "ripple", "usd-coin", "cardano", "dogecoin", "avalanche-2",
]


async def _coingecko_get(url: str, params: dict) -> Optional[dict]:
    """GET helper with 429 retry back-off."""
    for attempt in range(RETRY_ON_429 + 1):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, params=params)
                if r.status_code == 429:
                    if attempt < RETRY_ON_429:
                        logger.warning(
                            f"[CryptoAgent] CoinGecko 429 rate limit — "
                            f"retrying in {RETRY_DELAY_429}s (attempt {attempt+1})"
                        )
                        await asyncio.sleep(RETRY_DELAY_429)
                        continue
                    return None   # exhausted retries
                r.raise_for_status()
                return r.json()
        except Exception as e:
            if attempt < RETRY_ON_429:
                await asyncio.sleep(RETRY_DELAY_429)
                continue
            logger.error(f"[CryptoAgent] Request failed after retries: {e}")
            return None
    return None


class CryptoAgent(BaseAgent):
    name        = "crypto_agent"
    description = "Live cryptocurrency prices, market data, and trends via CoinGecko"

    async def run(self, parameters: dict, task_id: str, context: str = "") -> Optional[str]:
        try:
            return await self._run(parameters, task_id, context)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[CryptoAgent] Error: {e}", exc_info=True)
            return f"⚠️ CryptoAgent error: {e}"

    async def _run(self, parameters: dict, task_id: str, context: str) -> Optional[str]:
        query  = parameters.get("query") or context or ""
        ql     = query.lower()

        if any(k in ql for k in ["top 10", "top ten", "top coins", "best coins", "market cap"]):
            return await self._top_coins()
        if any(k in ql for k in ["trending", "hot coins", "what's popular"]):
            return await self._trending()

        coins = self._extract_coins(ql) or ["bitcoin", "ethereum"]
        return await self._prices(coins, query)

    # ── Prices ────────────────────────────────────────────────────────────────

    async def _prices(self, coin_ids: list[str], query: str = "") -> str:
        ids_str = ",".join(coin_ids)
        data = await _coingecko_get(
            f"{COINGECKO_BASE}/simple/price",
            {
                "ids":                 ids_str,
                "vs_currencies":       "usd,php",
                "include_24hr_change": "true",
                "include_market_cap":  "true",
                "include_24hr_vol":    "true",
            },
        )

        if data is None:
            return "⚠️ Could not fetch crypto prices. CoinGecko may be rate-limiting. Try again shortly."
        if not data:
            return "⚠️ No price data returned."

        lines = ["## 🪙 Cryptocurrency Prices\n"]
        for coin_id in coin_ids:
            if coin_id not in data:
                continue
            d         = data[coin_id]
            usd       = d.get("usd", 0)
            php       = d.get("php", 0)
            change    = d.get("usd_24h_change", 0) or 0
            mcap      = d.get("usd_market_cap", 0)
            arrow     = "▲" if change >= 0 else "▼"
            color_tag = "📈" if change >= 0 else "📉"

            name_display = coin_id.replace("-", " ").title()
            lines.append(
                f"**{name_display}** {color_tag}\n"
                f"  💵 USD: **${usd:,.4f}** {arrow} {abs(change):.2f}% (24h)\n"
                f"  🇵🇭 PHP: **₱{php:,.2f}**"
                + (f"\n  📊 Market Cap: ${mcap/1e9:.2f}B" if mcap > 1e9 else "")
            )

        lines.append("\n_Live data via CoinGecko_")
        return "\n\n".join(lines)

    # ── Top 10 ────────────────────────────────────────────────────────────────

    async def _top_coins(self) -> str:
        coins = await _coingecko_get(
            f"{COINGECKO_BASE}/coins/markets",
            {
                "vs_currency":             "usd",
                "order":                   "market_cap_desc",
                "per_page":                10,
                "page":                    1,
                "sparkline":               "false",
                "price_change_percentage": "24h",
            },
        )
        if coins is None:
            return "⚠️ Could not fetch top coins. CoinGecko may be rate-limiting."

        lines = ["## 🏆 Top 10 Cryptocurrencies by Market Cap\n"]
        for i, c in enumerate(coins, 1):
            change = c.get("price_change_percentage_24h") or 0
            arrow  = "▲" if change >= 0 else "▼"
            mcap   = c.get("market_cap", 0)
            lines.append(
                f"{i}. **{c['name']}** ({c['symbol'].upper()}) — "
                f"${c['current_price']:,.4f} {arrow}{abs(change):.2f}% | MC: ${mcap/1e9:.1f}B"
            )

        lines.append("\n_Live data via CoinGecko_")
        return "\n".join(lines)

    # ── Trending ──────────────────────────────────────────────────────────────

    async def _trending(self) -> str:
        data = await _coingecko_get(f"{COINGECKO_BASE}/search/trending", {})
        if data is None:
            return "⚠️ Could not fetch trending coins. CoinGecko may be rate-limiting."

        coins = data.get("coins", [])[:7]
        lines = ["## 🔥 Trending Cryptocurrencies (Last 24h)\n"]
        for i, item in enumerate(coins, 1):
            c = item.get("item", {})
            lines.append(
                f"{i}. **{c.get('name','')}** ({c.get('symbol','').upper()}) — "
                f"Rank #{c.get('market_cap_rank','?')}"
            )
        lines.append("\n_Trending data via CoinGecko_")
        return "\n".join(lines)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _extract_coins(self, query_lc: str) -> list[str]:
        import re
        found = []
        for alias, coin_id in COIN_ALIASES.items():
            if re.search(rf"\b{re.escape(alias)}\b", query_lc):
                if coin_id not in found:
                    found.append(coin_id)
        if not found:
            for alias, coin_id in COIN_ALIASES.items():
                if alias in query_lc and coin_id not in found:
                    found.append(coin_id)
        return found[:5]
