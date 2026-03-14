"""
AetherAI — Crypto Agent
Uses CoinGecko API (no key required for basic use, 30 req/min free).

Capabilities:
  • Live prices for any coin in USD and PHP
  • 24h price change percentage
  • Market cap
  • Top 10 coins by market cap
  • Trending coins

Trigger examples:
  "what is the price of bitcoin"
  "how much is ethereum in PHP"
  "crypto prices"
  "top 10 cryptocurrencies"
  "is bitcoin up or down today"
  "price of BTC ETH SOL"
"""

import asyncio
import logging
from typing import Optional

import httpx
from agents import BaseAgent

logger = logging.getLogger(__name__)

COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# Common coin name/symbol aliases → CoinGecko IDs
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
        query   = parameters.get("query") or context or ""
        ql      = query.lower()
        action  = parameters.get("action", "")

        # Detect intent
        if any(k in ql for k in ["top 10", "top ten", "top coins", "best coins", "market cap"]):
            return await self._top_coins()
        if any(k in ql for k in ["trending", "hot coins", "what's popular"]):
            return await self._trending()

        # Extract coin IDs from query
        coins = self._extract_coins(ql) or ["bitcoin", "ethereum"]
        return await self._prices(coins, query)

    # ── Prices ────────────────────────────────────────────────────────────────

    async def _prices(self, coin_ids: list[str], query: str = "") -> str:
        ids_str = ",".join(coin_ids)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    f"{COINGECKO_BASE}/simple/price",
                    params={
                        "ids":                  ids_str,
                        "vs_currencies":        "usd,php",
                        "include_24hr_change":  "true",
                        "include_market_cap":   "true",
                        "include_24hr_vol":     "true",
                    }
                )
                r.raise_for_status()
                data = r.json()

        except Exception as e:
            logger.error(f"[CryptoAgent] Price fetch failed: {e}")
            return f"⚠️ Could not fetch crypto prices. CoinGecko may be rate-limiting. Try again shortly."

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
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    f"{COINGECKO_BASE}/coins/markets",
                    params={
                        "vs_currency":           "usd",
                        "order":                 "market_cap_desc",
                        "per_page":              10,
                        "page":                  1,
                        "sparkline":             "false",
                        "price_change_percentage": "24h",
                    }
                )
                r.raise_for_status()
                coins = r.json()
        except Exception as e:
            return f"⚠️ Could not fetch top coins: {e}"

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
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{COINGECKO_BASE}/search/trending")
                r.raise_for_status()
                data  = r.json()
                coins = data.get("coins", [])[:7]
        except Exception as e:
            return f"⚠️ Could not fetch trending coins: {e}"

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
        found = []
        for alias, coin_id in COIN_ALIASES.items():
            import re
            if re.search(rf"\b{re.escape(alias)}\b", query_lc):
                if coin_id not in found:
                    found.append(coin_id)
        if not found:
            # Check if any coin name appears as a substring
            for alias, coin_id in COIN_ALIASES.items():
                if alias in query_lc and coin_id not in found:
                    found.append(coin_id)
        return found[:5]  # max 5 coins per query
