"""
AetherAI — Finance Agent  (patch 8)

Fix: "convert 1000 USD to PHP" was showing exchange rates instead of
the actual conversion result. Root cause: the "convert" keyword in
step 3 of _run() matched BEFORE the proper _parse_conversion result
was used. Also added "php" as a currency alias so it's always
recognized even when not normalized.
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
    "peso": "PHP",   "pesos": "PHP",  "philippine peso": "PHP",
    "php": "PHP",                                            # FIX: add php
    "dollar": "USD", "dollars": "USD", "usd": "USD",
    "euro": "EUR",   "euros": "EUR",   "eur": "EUR",
    "pound": "GBP",  "pounds": "GBP",  "gbp": "GBP",
    "yen": "JPY",    "jpy": "JPY",
    "yuan": "CNY",   "renminbi": "CNY","cny": "CNY",
    "won": "KRW",    "krw": "KRW",
    "rupee": "INR",  "rupees": "INR",  "inr": "INR",
    "baht": "THB",   "thb": "THB",
    "ringgit": "MYR","myr": "MYR",
    "rupiah": "IDR", "idr": "IDR",
    "sgd": "SGD",    "singapore dollar": "SGD",
    "aud": "AUD",    "australian dollar": "AUD",
    "cad": "CAD",    "canadian dollar": "CAD",
}

CURRENCY_CODES = {
    "USD","EUR","GBP","JPY","CHF","CAD","AUD","NZD","PHP","SGD","HKD",
    "CNY","KRW","INR","IDR","THB","MYR","VND","MXN","BRL","ARS","ZAR",
    "NOK","SEK","DKK","PLN","CZK","HUF","TRY","RUB","SAR","AED","QAR",
    "KWD","BHD","OMR","EGP","NGN","GHS","KES","PKR","BDT","LKR","MMK",
}

TICKER_ALIASES = {
    "apple":"AAPL",   "tesla":"TSLA",   "google":"GOOGL",
    "alphabet":"GOOGL","amazon":"AMZN", "microsoft":"MSFT",
    "meta":"META",    "nvidia":"NVDA",   "netflix":"NFLX",
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

        # 1. Explicit conversion with amount — MUST come first
        amount, from_cur, to_cur = self._parse_conversion(ql)
        if from_cur and to_cur:
            logger.info(f"[FinanceAgent] Converting {amount} {from_cur} → {to_cur}")
            return await self._convert(amount, from_cur, to_cur)

        # 2. Exchange rate pair without amount
        from_cur2, to_cur2 = self._parse_rate_query(ql)
        if from_cur2 and to_cur2:
            return await self._exchange_rate(from_cur2, to_cur2)

        # 3. Stock query
        if self._is_stock_query(ql):
            ticker = self._extract_ticker(ql)
            if ticker:
                return await self._stock_price(ticker)

        # 4. General currency keywords without specific pair
        currencies = self._extract_currencies(ql)
        if len(currencies) >= 2:
            return await self._exchange_rate(currencies[0], currencies[1])
        if len(currencies) == 1:
            return await self._exchange_rate(currencies[0], "PHP")

        # Default
        return await self._exchange_rate("USD", "PHP")

    # ── Currency Conversion ────────────────────────────────────────────────────

    async def _convert(self, amount: float, from_cur: str, to_cur: str) -> str:
        rates = await self._fetch_rates(from_cur)
        if not rates:
            return "⚠️ Could not fetch exchange rates. Try again shortly."
        if to_cur not in rates:
            return f"⚠️ Currency '{to_cur}' not found in rates."

        rate      = rates[to_cur]
        converted = amount * rate

        lines = [
            f"## 💱 Currency Conversion",
            f"",
            f"**{amount:,.2f} {from_cur}** = **{converted:,.2f} {to_cur}**",
            f"Rate: 1 {from_cur} = {rate:.6f} {to_cur}",
        ]

        # Show additional useful rates
        extras = []
        if from_cur != "PHP" and to_cur != "PHP" and "PHP" in rates:
            extras.append(f"🇵🇭 PHP equivalent: **₱{amount * rates['PHP']:,.2f}**")
        if from_cur != "USD" and to_cur != "USD" and "USD" in rates:
            extras.append(f"💵 USD equivalent: **${amount * rates['USD']:,.2f}**")

        if extras:
            lines.append("")
            lines.extend(extras)

        lines.append("")
        lines.append("_Live rates via ExchangeRate-API_")
        return "\n".join(lines)

    async def _exchange_rate(self, from_cur: str, to_cur: str) -> str:
        rates = await self._fetch_rates(from_cur)
        if not rates:
            return "⚠️ Could not fetch exchange rates."

        show  = [to_cur] + [c for c in ["USD","EUR","GBP","JPY","PHP","SGD","AUD","CNY"]
                             if c != from_cur and c != to_cur][:6]
        lines = [f"## 💱 Exchange Rates — 1 {from_cur}", ""]
        for cur in show:
            if cur in rates:
                lines.append(f"1 {from_cur} = **{rates[cur]:.4f} {cur}**")
        lines.append("")
        lines.append("_Live rates via ExchangeRate-API_")
        return "\n".join(lines)

    async def _fetch_rates(self, base: str) -> Optional[dict]:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(EXCHANGE_RATE_URL.format(base=base))
                r.raise_for_status()
                data = r.json()
                if data.get("result") == "success":
                    return data.get("rates", {})
                logger.warning(f"[FinanceAgent] ExchangeRate-API error: {data.get('error-type')}")
        except Exception as e:
            logger.warning(f"[FinanceAgent] Fetch rates failed: {e}")
        return None

    # ── Stock Prices ───────────────────────────────────────────────────────────

    async def _stock_price(self, ticker: str) -> str:
        if not self.av_key:
            return (
                f"## 📈 Stock: {ticker}\n\n"
                f"⚠️ Stock lookup requires an Alpha Vantage API key.\n"
                f"Get yours free at: https://www.alphavantage.co/support/#api-key\n\n"
                f"Add to Railway variables: `ALPHAVANTAGE_API_KEY = your_key`"
            )
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(ALPHA_VANTAGE_URL, params={
                    "function":"GLOBAL_QUOTE","symbol":ticker,"apikey":self.av_key
                })
                r.raise_for_status()
                quote_data = r.json().get("Global Quote", {})
        except Exception as e:
            return f"⚠️ Could not fetch stock data for {ticker}: {e}"

        if not quote_data or not quote_data.get("05. price"):
            return f"⚠️ No data for **{ticker}**. Check the symbol is correct."

        price   = float(quote_data.get("05. price", 0))
        open_p  = float(quote_data.get("02. open", 0))
        high    = float(quote_data.get("03. high", 0))
        low     = float(quote_data.get("04. low", 0))
        prev    = float(quote_data.get("08. previous close", 0))
        change  = float(quote_data.get("09. change", 0))
        changep = float(quote_data.get("10. change percent", "0%").replace("%",""))
        vol     = int(quote_data.get("06. volume", 0))
        date    = quote_data.get("07. latest trading day", "")
        arrow   = "▲" if change >= 0 else "▼"
        icon    = "📈" if change >= 0 else "📉"

        return (
            f"## {icon} {ticker} Stock Price\n\n"
            f"**Current: ${price:,.2f}** {arrow} {abs(changep):.2f}% (${abs(change):,.2f})\n\n"
            f"📅 Date: {date}\n"
            f"🔓 Open: ${open_p:,.2f} | ⬆️ High: ${high:,.2f} | ⬇️ Low: ${low:,.2f}\n"
            f"🔒 Prev Close: ${prev:,.2f} | 📊 Volume: {vol:,}\n\n"
            f"_Data via Alpha Vantage_"
        )

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _normalize_currencies(self, text: str) -> str:
        result = text
        for alias, code in sorted(CURRENCY_ALIASES.items(), key=lambda x: -len(x[0])):
            result = re.sub(rf"\b{re.escape(alias)}\b", code, result, flags=re.IGNORECASE)
        return result

    def _parse_conversion(self, ql: str) -> tuple:
        normalized = self._normalize_currencies(ql).upper()
        logger.debug(f"[FinanceAgent] parse_conversion normalized: '{normalized}'")

        patterns = [
            r"CONVERT\s+([\d,\.]+)\s+([A-Z]{3})\s+TO\s+([A-Z]{3})",
            r"([\d,\.]+)\s+([A-Z]{3})\s+(?:TO|IN|INTO)\s+([A-Z]{3})",
            r"HOW\s+MUCH\s+IS\s+([\d,\.]+)\s+([A-Z]{3})\s+IN\s+([A-Z]{3})",
            r"([\d,\.]+)\s+([A-Z]{3})\s+TO\s+([A-Z]{3})",
        ]
        for pat in patterns:
            m = re.search(pat, normalized)
            if m:
                try:
                    amount = float(m.group(1).replace(",", ""))
                    fc, tc = m.group(2), m.group(3)
                    if fc in CURRENCY_CODES and tc in CURRENCY_CODES:
                        logger.info(f"[FinanceAgent] Matched conversion: {amount} {fc} → {tc}")
                        return amount, fc, tc
                except (ValueError, IndexError):
                    pass
        return 1.0, None, None

    def _parse_rate_query(self, ql: str) -> tuple:
        normalized = self._normalize_currencies(ql).upper()
        m = re.search(r"\b([A-Z]{3})\s+(?:TO|IN)\s+([A-Z]{3})\b", normalized)
        if m:
            fc, tc = m.group(1), m.group(2)
            if fc in CURRENCY_CODES and tc in CURRENCY_CODES:
                return fc, tc
        return None, None

    def _extract_currencies(self, ql: str) -> list:
        normalized = self._normalize_currencies(ql).upper()
        found = []
        for m in re.finditer(r"\b([A-Z]{3})\b", normalized):
            code = m.group(1)
            if code in CURRENCY_CODES and code not in found:
                found.append(code)
        return found

    def _is_stock_query(self, ql: str) -> bool:
        kw = ["stock","share price","nasdaq","nyse","ticker","stock price","stock market"]
        if any(k in ql for k in kw): return True
        return any(name in ql for name in TICKER_ALIASES)

    def _extract_ticker(self, ql: str) -> Optional[str]:
        for name, ticker in TICKER_ALIASES.items():
            if name in ql:
                return ticker
        m = re.search(r"\b([A-Z]{2,5})\b", ql.upper())
        if m:
            word = m.group(1)
            excluded = CURRENCY_CODES | {"THE","FOR","AND","BUT","NOT","ALL",
                                          "TOP","HOW","ARE","GET","SET","NEW",
                                          "BTC","ETH","SOL","BNB","XRP","ADA"}
            if word not in excluded:
                return word
        return None
