"""
AetherAI — Finance Agent
Currency: ExchangeRate-API (no API key needed, free forever)
Stocks:   Alpha Vantage (free API key, 25 req/day — ALPHAVANTAGE_API_KEY in env)

Capabilities:
  • Real-time currency conversion (all currencies, including PHP)
  • Live exchange rates
  • Stock price lookup (NASDAQ, NYSE)
  • Basic stock info (open, high, low, volume)

Trigger examples:
  "convert 500 USD to PHP"
  "how much is 1 dollar in pesos"
  "exchange rate USD to EUR"
  "what is the Apple stock price"
  "AAPL stock today"
  "NASDAQ: GOOGL"
  "what is Tesla stock price"
"""

import asyncio
import logging
import os
import re
from typing import Optional

import httpx
from agents import BaseAgent

logger = logging.getLogger(__name__)

EXCHANGE_RATE_URL = "https://open.er-api.com/v6/latest/{base}"
ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"

# Currency aliases
CURRENCY_ALIASES = {
    "peso": "PHP", "pesos": "PHP", "philippine peso": "PHP",
    "dollar": "USD", "dollars": "USD", "usd": "USD",
    "euro": "EUR", "euros": "EUR",
    "pound": "GBP", "pounds": "GBP",
    "yen": "JPY",
    "yuan": "CNY", "renminbi": "CNY",
    "won": "KRW",
    "rupee": "INR", "rupees": "INR",
    "baht": "THB",
    "ringgit": "MYR",
    "rupiah": "IDR",
    "sgd": "SGD", "singapore dollar": "SGD",
    "aud": "AUD", "australian dollar": "AUD",
    "cad": "CAD", "canadian dollar": "CAD",
}

# Common stock tickers
TICKER_ALIASES = {
    "apple":     "AAPL", "tesla":   "TSLA", "google":    "GOOGL",
    "alphabet":  "GOOGL", "amazon": "AMZN", "microsoft": "MSFT",
    "meta":      "META",  "nvidia": "NVDA",  "netflix":  "NFLX",
    "samsung":   "005930.KS",
}


class FinanceAgent(BaseAgent):
    name        = "finance_agent"
    description = "Currency conversion, exchange rates, and stock prices"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.av_key = os.getenv("ALPHAVANTAGE_API_KEY", "")

    async def run(self, parameters: dict, task_id: str, context: str = "") -> Optional[str]:
        try:
            return await self._run(parameters, task_id, context)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[FinanceAgent] Error: {e}", exc_info=True)
            return f"⚠️ FinanceAgent error: {e}"

    async def _run(self, parameters: dict, task_id: str, context: str) -> Optional[str]:
        query = parameters.get("query") or context or ""
        ql    = query.lower()

        # Route: stock or currency
        if self._is_stock_query(ql):
            ticker = self._extract_ticker(ql)
            if ticker:
                return await self._stock_price(ticker)
            return "⚠️ Could not identify a stock ticker in your query. Try: 'Apple stock price' or 'AAPL stock'"

        # Currency conversion
        amount, from_cur, to_cur = self._parse_conversion(ql)
        if from_cur and to_cur:
            return await self._convert(amount, from_cur, to_cur)

        # Exchange rate (no amount specified)
        from_cur2, to_cur2 = self._parse_rate_query(ql)
        if from_cur2 and to_cur2:
            return await self._exchange_rate(from_cur2, to_cur2)

        # Default: show USD rates
        return await self._exchange_rate("USD", "PHP")

    # ── Currency Conversion ───────────────────────────────────────────────────

    async def _convert(self, amount: float, from_cur: str, to_cur: str) -> str:
        rates = await self._fetch_rates(from_cur)
        if not rates:
            return f"⚠️ Could not fetch exchange rates. Try again shortly."

        if to_cur not in rates:
            return f"⚠️ Currency '{to_cur}' not found in rates."

        rate        = rates[to_cur]
        converted   = amount * rate
        # Also show PHP if neither is PHP
        extra = ""
        if from_cur != "PHP" and to_cur != "PHP" and "PHP" in rates:
            php_amount = amount * rates["PHP"]
            extra = f"\n  🇵🇭 PHP equivalent: **₱{php_amount:,.2f}**"

        return (
            f"## 💱 Currency Conversion\n\n"
            f"**{amount:,.2f} {from_cur}** = **{converted:,.4f} {to_cur}**\n"
            f"  Rate: 1 {from_cur} = {rate:.6f} {to_cur}"
            f"{extra}\n\n"
            f"_Live rates via ExchangeRate-API_"
        )

    async def _exchange_rate(self, from_cur: str, to_cur: str) -> str:
        rates = await self._fetch_rates(from_cur)
        if not rates:
            return "⚠️ Could not fetch exchange rates."

        # Show a useful set of rates
        show_pairs = [to_cur] + [c for c in ["USD","EUR","GBP","JPY","PHP","SGD","AUD"]
                                  if c != from_cur and c != to_cur][:5]
        lines = [f"## 💱 Exchange Rates — {from_cur}\n"]
        for cur in show_pairs:
            if cur in rates:
                lines.append(f"  1 {from_cur} = **{rates[cur]:.4f} {cur}**")

        lines.append(f"\n_Live rates via ExchangeRate-API_")
        return "\n".join(lines)

    async def _fetch_rates(self, base: str) -> Optional[dict]:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(EXCHANGE_RATE_URL.format(base=base))
                r.raise_for_status()
                data = r.json()
                if data.get("result") == "success":
                    return data.get("rates", {})
        except Exception as e:
            logger.warning(f"[FinanceAgent] Exchange rate fetch failed: {e}")
        return None

    # ── Stock Prices ──────────────────────────────────────────────────────────

    async def _stock_price(self, ticker: str) -> str:
        if not self.av_key:
            return (
                f"## 📈 Stock: {ticker}\n\n"
                f"⚠️ Stock price lookup requires an Alpha Vantage API key.\n"
                f"Get a free key at: https://www.alphavantage.co/support/#api-key\n"
                f"Then add to Railway env vars: `ALPHAVANTAGE_API_KEY=your_key`"
            )

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(ALPHA_VANTAGE_URL, params={
                    "function": "GLOBAL_QUOTE",
                    "symbol":   ticker,
                    "apikey":   self.av_key,
                })
                r.raise_for_status()
                data  = r.json()
                quote = data.get("Global Quote", {})

        except Exception as e:
            return f"⚠️ Could not fetch stock data for {ticker}: {e}"

        if not quote or not quote.get("05. price"):
            return (
                f"⚠️ No data for ticker **{ticker}**. "
                f"Check the symbol is correct (e.g. AAPL, TSLA, GOOGL)."
            )

        price   = float(quote.get("05. price", 0))
        open_p  = float(quote.get("02. open", 0))
        high    = float(quote.get("03. high", 0))
        low     = float(quote.get("04. low", 0))
        prev    = float(quote.get("08. previous close", 0))
        change  = float(quote.get("09. change", 0))
        changep = float(quote.get("10. change percent", "0%").replace("%",""))
        vol     = int(quote.get("06. volume", 0))
        date    = quote.get("07. latest trading day", "")

        arrow = "▲" if change >= 0 else "▼"
        icon  = "📈" if change >= 0 else "📉"

        return (
            f"## {icon} {ticker} Stock Price\n\n"
            f"**Current: ${price:,.2f}** {arrow} {abs(changep):.2f}% (${abs(change):,.2f})\n\n"
            f"  📅 Date: {date}\n"
            f"  🔓 Open: ${open_p:,.2f}\n"
            f"  ⬆️  High: ${high:,.2f}\n"
            f"  ⬇️  Low:  ${low:,.2f}\n"
            f"  🔒 Prev Close: ${prev:,.2f}\n"
            f"  📊 Volume: {vol:,}\n\n"
            f"_Data via Alpha Vantage_"
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _is_stock_query(self, ql: str) -> bool:
        kw = ["stock", "share price", "nasdaq", "nyse", "ticker",
              "stock price", "share", "market price"]
        if any(k in ql for k in kw): return True
        for name in TICKER_ALIASES:
            if name in ql: return True
        # Looks like a raw ticker (2-5 uppercase letters)
        if re.search(r"\b[A-Z]{2,5}\b", ql.upper()):
            return True
        return False

    def _extract_ticker(self, ql: str) -> Optional[str]:
        # Check company name aliases first
        for name, ticker in TICKER_ALIASES.items():
            if name in ql:
                return ticker
        # Raw ticker pattern
        m = re.search(r"\b([A-Z]{2,5})\b", ql.upper())
        if m:
            # Filter out common English words that match pattern
            word = m.group(1)
            excluded = {"USD","PHP","EUR","GBP","THE","FOR","AND","BUT","NOT",
                        "BTC","ETH","ALL","TOP","HOW","ARE","GET","SET","NEW"}
            if word not in excluded:
                return word
        return None

    def _parse_conversion(self, ql: str) -> tuple:
        """Parse 'convert 500 USD to PHP' or '1000 dollars to pesos'."""
        # Normalize aliases
        normalized = ql
        for alias, code in CURRENCY_ALIASES.items():
            normalized = re.sub(rf"\b{alias}\b", code, normalized, flags=re.IGNORECASE)

        patterns = [
            r"convert\s+([\d,\.]+)\s+([A-Z]{3})\s+to\s+([A-Z]{3})",
            r"([\d,\.]+)\s+([A-Z]{3})\s+(?:to|in|into)\s+([A-Z]{3})",
            r"how\s+much\s+is\s+([\d,\.]+)\s+([A-Z]{3})\s+in\s+([A-Z]{3})",
        ]
        for pat in patterns:
            m = re.search(pat, normalized.upper())
            if m:
                try:
                    amount = float(m.group(1).replace(",", ""))
                    return amount, m.group(2), m.group(3)
                except ValueError:
                    pass
        return 1.0, None, None

    def _parse_rate_query(self, ql: str) -> tuple:
        """Parse 'exchange rate USD to PHP'."""
        normalized = ql
        for alias, code in CURRENCY_ALIASES.items():
            normalized = re.sub(rf"\b{alias}\b", code, normalized, flags=re.IGNORECASE)

        m = re.search(r"([A-Z]{3})\s+(?:to|in)\s+([A-Z]{3})", normalized.upper())
        if m:
            return m.group(1), m.group(2)
        return None, None
