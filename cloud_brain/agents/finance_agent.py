"""
AetherAI — Finance Agent  (patch 5)

Fix: `_is_stock_query()` was matching currency codes like "PHP" and "USD"
as stock tickers (the regex `\b[A-Z]{2,5}\b` catches any uppercase word).
This caused "convert 1000 USD to PHP" to be routed to stock lookup instead
of currency conversion.

Fix: check for currency conversion FIRST in `_run()`, before stock check.
Also added currency code exclusion list to `_extract_ticker()`.
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

CURRENCY_ALIASES = {
    "peso": "PHP",   "pesos": "PHP",     "philippine peso": "PHP",
    "dollar": "USD", "dollars": "USD",   "usd": "USD",
    "euro": "EUR",   "euros": "EUR",
    "pound": "GBP",  "pounds": "GBP",
    "yen": "JPY",    "yuan": "CNY",      "renminbi": "CNY",
    "won": "KRW",    "rupee": "INR",     "rupees": "INR",
    "baht": "THB",   "ringgit": "MYR",   "rupiah": "IDR",
    "sgd": "SGD",    "singapore dollar": "SGD",
    "aud": "AUD",    "australian dollar": "AUD",
    "cad": "CAD",    "canadian dollar": "CAD",
}

# All ISO 4217 currency codes that should NEVER be treated as stock tickers
CURRENCY_CODES = {
    "USD","EUR","GBP","JPY","CHF","CAD","AUD","NZD","PHP","SGD","HKD",
    "CNY","KRW","INR","IDR","THB","MYR","VND","MXN","BRL","ARS","ZAR",
    "NOK","SEK","DKK","PLN","CZK","HUF","TRY","RUB","SAR","AED","QAR",
    "KWD","BHD","OMR","EGP","NGN","GHS","KES","TZS","UGX","ETB","XOF",
    "XAF","MAD","DZD","TND","LYD","PKR","BDT","LKR","MMK","KHR","LAK",
    "MNT","UZS","KZT","UAH","GEL","AMD","AZN","BYN","MDL","ALL","MKD",
    "HRK","BAM","RSD","BGN","RON","ISK","HRK","LTL","LVL","EEK",
}

TICKER_ALIASES = {
    "apple":"AAPL",    "tesla":"TSLA",    "google":"GOOGL",
    "alphabet":"GOOGL","amazon":"AMZN",   "microsoft":"MSFT",
    "meta":"META",     "nvidia":"NVDA",   "netflix":"NFLX",
    "samsung":"005930.KS",
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

        # FIX: Check currency FIRST — before stock check.
        # "convert 1000 USD to PHP" was being caught by _is_stock_query
        # because PHP/USD match the \b[A-Z]{2,5}\b ticker regex.

        # 1. Explicit conversion with amount
        amount, from_cur, to_cur = self._parse_conversion(ql)
        if from_cur and to_cur:
            return await self._convert(amount, from_cur, to_cur)

        # 2. Exchange rate without amount
        from_cur2, to_cur2 = self._parse_rate_query(ql)
        if from_cur2 and to_cur2:
            return await self._exchange_rate(from_cur2, to_cur2)

        # 3. General "exchange rate" or currency keyword without specific pair
        if any(k in ql for k in ["exchange rate","currency rate","forex","usd to php",
                                   "php to usd","dollar to peso","peso to dollar",
                                   "convert","how much is"]):
            # Try to find any currency pair
            currencies = self._extract_currencies(ql)
            if len(currencies) >= 2:
                return await self._exchange_rate(currencies[0], currencies[1])
            if len(currencies) == 1:
                return await self._exchange_rate(currencies[0], "PHP")
            return await self._exchange_rate("USD", "PHP")

        # 4. Stock query (checked AFTER currency)
        if self._is_stock_query(ql):
            ticker = self._extract_ticker(ql)
            if ticker:
                return await self._stock_price(ticker)
            return (
                "⚠️ Could not identify a stock ticker. "
                "Try: 'Apple stock price', 'TSLA stock', or 'AAPL'"
            )

        # Default: show USD/PHP rate
        return await self._exchange_rate("USD", "PHP")

    # ── Currency Conversion ───────────────────────────────────────────────────

    async def _convert(self, amount: float, from_cur: str, to_cur: str) -> str:
        rates = await self._fetch_rates(from_cur)
        if not rates:
            return "⚠️ Could not fetch exchange rates. Try again shortly."
        if to_cur not in rates:
            return f"⚠️ Currency '{to_cur}' not found."

        rate      = rates[to_cur]
        converted = amount * rate
        extra     = ""
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

        show = [to_cur] + [c for c in ["USD","EUR","GBP","JPY","PHP","SGD","AUD","CNY"]
                            if c != from_cur and c != to_cur][:6]
        lines = [f"## 💱 Exchange Rates — 1 {from_cur}\n"]
        for cur in show:
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
                logger.warning(f"[FinanceAgent] ExchangeRate API error: {data}")
        except Exception as e:
            logger.warning(f"[FinanceAgent] Exchange rate fetch failed: {e}")
        return None

    # ── Stock Prices ──────────────────────────────────────────────────────────

    async def _stock_price(self, ticker: str) -> str:
        if not self.av_key:
            return (
                f"## 📈 Stock: {ticker}\n\n"
                f"⚠️ Stock lookup requires an Alpha Vantage API key.\n"
                f"Get yours free at: https://www.alphavantage.co/support/#api-key\n\n"
                f"Then add to Railway variables:\n"
                f"  `ALPHAVANTAGE_API_KEY = your_key_here`"
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
                f"⚠️ No data for **{ticker}**. "
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
        arrow   = "▲" if change >= 0 else "▼"
        icon    = "📈" if change >= 0 else "📉"

        return (
            f"## {icon} {ticker} Stock Price\n\n"
            f"**Current: ${price:,.2f}** {arrow} {abs(changep):.2f}% (${abs(change):,.2f})\n\n"
            f"  📅 Date: {date}\n"
            f"  🔓 Open: ${open_p:,.2f}\n"
            f"  ⬆️  High: ${high:,.2f}\n"
            f"  ⬇️  Low:  ${low:,.2f}\n"
            f"  🔒 Prev: ${prev:,.2f}\n"
            f"  📊 Volume: {vol:,}\n\n"
            f"_Data via Alpha Vantage_"
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _is_stock_query(self, ql: str) -> bool:
        kw = ["stock","share price","nasdaq","nyse","ticker","stock price","stock market"]
        if any(k in ql for k in kw): return True
        for name in TICKER_ALIASES:
            if name in ql: return True
        return False

    def _extract_ticker(self, ql: str) -> Optional[str]:
        # Named company first
        for name, ticker in TICKER_ALIASES.items():
            if name in ql:
                return ticker
        # Raw ticker — exclude currency codes
        m = re.search(r"\b([A-Z]{2,5})\b", ql.upper())
        if m:
            word = m.group(1)
            excluded = CURRENCY_CODES | {"THE","FOR","AND","BUT","NOT","ALL",
                                          "TOP","HOW","ARE","GET","SET","NEW",
                                          "BTC","ETH","SOL","BNB","XRP","ADA"}
            if word not in excluded:
                return word
        return None

    def _extract_currencies(self, ql: str) -> list[str]:
        """Extract currency codes from a query, checking aliases first."""
        normalized = ql
        for alias, code in CURRENCY_ALIASES.items():
            normalized = re.sub(rf"\b{alias}\b", code, normalized, flags=re.IGNORECASE)
        found = []
        for m in re.finditer(r"\b([A-Z]{3})\b", normalized.upper()):
            code = m.group(1)
            if code in CURRENCY_CODES and code not in found:
                found.append(code)
        return found

    def _parse_conversion(self, ql: str) -> tuple:
        normalized = ql
        for alias, code in CURRENCY_ALIASES.items():
            normalized = re.sub(rf"\b{alias}\b", code, normalized, flags=re.IGNORECASE)
        patterns = [
            r"convert\s+([\d,\.]+)\s+([A-Z]{3})\s+to\s+([A-Z]{3})",
            r"([\d,\.]+)\s+([A-Z]{3})\s+(?:to|in|into)\s+([A-Z]{3})",
            r"how\s+much\s+is\s+([\d,\.]+)\s+([A-Z]{3})\s+in\s+([A-Z]{3})",
            r"([\d,\.]+)\s+([A-Z]{3})\s+(?:to|in)\s+([A-Z]{3})",
        ]
        for pat in patterns:
            m = re.search(pat, normalized.upper())
            if m:
                try:
                    amount = float(m.group(1).replace(",",""))
                    fc, tc = m.group(2), m.group(3)
                    if fc in CURRENCY_CODES and tc in CURRENCY_CODES:
                        return amount, fc, tc
                except ValueError:
                    pass
        return 1.0, None, None

    def _parse_rate_query(self, ql: str) -> tuple:
        normalized = ql
        for alias, code in CURRENCY_ALIASES.items():
            normalized = re.sub(rf"\b{alias}\b", code, normalized, flags=re.IGNORECASE)
        m = re.search(r"([A-Z]{3})\s+(?:to|in)\s+([A-Z]{3})", normalized.upper())
        if m:
            fc, tc = m.group(1), m.group(2)
            if fc in CURRENCY_CODES and tc in CURRENCY_CODES:
                return fc, tc
        return None, None
