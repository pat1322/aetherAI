"""
AetherAI — Weather Agent  (patch 8)

Fix: Open-Meteo fails on Railway. Added wttr.in as primary fallback.
wttr.in is extremely reliable, no key needed, global coverage.
"""

import asyncio
import logging
from typing import Optional
from urllib.parse import quote

import httpx
from agents import BaseAgent

logger = logging.getLogger(__name__)

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL   = "https://api.open-meteo.com/v1/forecast"
WTTR_URL      = "https://wttr.in/{city}?format=j1"

WMO_CODES = {
    0: "Clear sky ☀️",    1: "Mainly clear 🌤️",  2: "Partly cloudy ⛅",  3: "Overcast ☁️",
    45: "Foggy 🌫️",       48: "Icy fog 🌫️",
    51: "Light drizzle 🌦️", 53: "Drizzle 🌦️",    55: "Heavy drizzle 🌦️",
    61: "Light rain 🌧️",  63: "Rain 🌧️",          65: "Heavy rain 🌧️",
    71: "Light snow 🌨️",  73: "Snow 🌨️",           75: "Heavy snow 🌨️",
    80: "Showers 🌦️",     81: "Heavy showers 🌦️",  82: "Violent showers ⛈️",
    95: "Thunderstorm ⛈️", 96: "Thunderstorm w/ hail ⛈️",
}

WTTR_CODES = {
    "113": "Clear sky ☀️",   "116": "Partly cloudy ⛅",  "119": "Cloudy ☁️",
    "122": "Overcast ☁️",    "143": "Mist 🌫️",           "176": "Patchy rain 🌦️",
    "185": "Patchy sleet 🌨️","200": "Thundery showers ⛈️","227": "Blowing snow 🌨️",
    "230": "Blizzard 🌨️",    "248": "Fog 🌫️",             "260": "Freezing fog 🌫️",
    "263": "Patchy light drizzle 🌦️","266": "Light drizzle 🌦️",
    "281": "Freezing drizzle 🌦️","284": "Heavy freezing drizzle 🌦️",
    "293": "Patchy light rain 🌧️","296": "Light rain 🌧️",
    "299": "Moderate rain 🌧️","302": "Heavy rain 🌧️",
    "305": "Heavy rain 🌧️",  "308": "Torrential rain 🌧️",
    "353": "Light rain showers 🌦️","356": "Moderate rain showers 🌧️",
    "359": "Torrential showers 🌧️","389": "Heavy rain & thunder ⛈️",
    "395": "Heavy snow & thunder ⛈️",
}

DEFAULT_CITY = "Manila"

PH_CITY_FALLBACK = {
    "manila":  (14.5995, 120.9842, "Manila, Philippines",        "Asia/Manila"),
    "cebu":    (10.3157, 123.8854, "Cebu, Philippines",          "Asia/Manila"),
    "davao":   (7.1907,  125.4553, "Davao, Philippines",         "Asia/Manila"),
    "quezon":  (14.6760, 121.0437, "Quezon City, Philippines",   "Asia/Manila"),
    "makati":  (14.5547, 121.0244, "Makati, Philippines",        "Asia/Manila"),
    "pasig":   (14.5764, 121.0851, "Pasig, Philippines",         "Asia/Manila"),
    "taguig":  (14.5176, 121.0509, "Taguig, Philippines",        "Asia/Manila"),
    "baguio":  (16.4023, 120.5960, "Baguio, Philippines",        "Asia/Manila"),
    "iloilo":  (10.7202, 122.5621, "Iloilo, Philippines",        "Asia/Manila"),
    "cagayan": (8.4542,  124.6319, "Cagayan de Oro, Philippines","Asia/Manila"),
}


class WeatherAgent(BaseAgent):
    name        = "weather_agent"
    description = "Real-time weather and forecasts using Open-Meteo + wttr.in fallback"

    async def run(self, parameters: dict, task_id: str, context: str = "") -> Optional[str]:
        try:
            return await self._run(parameters, task_id, context)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[WeatherAgent] Error: {e}", exc_info=True)
            return f"⚠️ WeatherAgent error: {e}"

    async def _run(self, parameters: dict, task_id: str, context: str) -> Optional[str]:
        query    = parameters.get("query") or context or ""
        city     = parameters.get("city") or self._extract_city(query) or DEFAULT_CITY
        forecast = parameters.get("forecast", False) or self._wants_forecast(query)

        logger.info(f"[WeatherAgent] city='{city}' forecast={forecast}")

        # Try Open-Meteo first (better data), fallback to wttr.in
        result = await self._try_open_meteo(city, forecast)
        if result:
            return result

        logger.info(f"[WeatherAgent] Open-Meteo failed, trying wttr.in for '{city}'")
        result = await self._try_wttr(city, forecast)
        if result:
            return result

        return (
            f"⚠️ Could not fetch weather data for **{city}**.\n"
            f"Both Open-Meteo and wttr.in are unavailable. "
            f"Please check https://wttr.in/{quote(city)} directly."
        )

    # ── Open-Meteo path ───────────────────────────────────────────────────────

    async def _try_open_meteo(self, city: str, forecast: bool) -> Optional[str]:
        lat, lon, full_city, timezone = await self._geocode(city)
        if lat is None:
            return None

        weather = await self._fetch_open_meteo(lat, lon, timezone, include_daily=forecast)
        if not weather:
            return None

        return self._format_open_meteo(full_city, weather, forecast)

    async def _geocode(self, city: str) -> tuple:
        city_lc = city.lower().strip()
        for key, fallback in PH_CITY_FALLBACK.items():
            if key in city_lc:
                return fallback

        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                r = await client.get(GEOCODING_URL, params={
                    "name": city, "count": 1, "language": "en", "format": "json"
                })
                r.raise_for_status()
                data = r.json()

            results = data.get("results", [])
            if not results:
                return None, None, city, "UTC"

            loc      = results[0]
            tz       = loc.get("timezone", "UTC")
            full     = f"{loc.get('name', city)}, {loc.get('country', '')}"
            return loc["latitude"], loc["longitude"], full.strip(", "), tz

        except Exception as e:
            logger.warning(f"[WeatherAgent] Geocoding failed: {e}")
            return None, None, city, "UTC"

    async def _fetch_open_meteo(self, lat, lon, timezone, include_daily=True) -> Optional[dict]:
        params = {
            "latitude": lat, "longitude": lon, "timezone": timezone,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                       "weathercode,windspeed_10m,winddirection_10m,precipitation,cloudcover",
        }
        if include_daily:
            params["daily"] = ("weathercode,temperature_2m_max,temperature_2m_min,"
                               "precipitation_sum,windspeed_10m_max,sunrise,sunset")
            params["forecast_days"] = 7
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(WEATHER_URL, params=params)
                r.raise_for_status()
                data = r.json()
                if not data.get("current"):
                    return None
                return data
        except Exception as e:
            logger.warning(f"[WeatherAgent] Open-Meteo failed: {e}")
            return None

    def _format_open_meteo(self, city: str, data: dict, include_forecast: bool) -> str:
        cur  = data.get("current", {})
        temp = cur.get("temperature_2m", "?")
        feels = cur.get("apparent_temperature", "?")
        hum  = cur.get("relative_humidity_2m", "?")
        wind = cur.get("windspeed_10m", "?")
        wdir = cur.get("winddirection_10m", 0)
        prec = cur.get("precipitation", 0) or 0
        code = cur.get("weathercode", 0)
        cond = WMO_CODES.get(code, "Unknown")

        lines = [
            f"## 🌍 Weather — {city}",
            f"**{cond}**",
            f"🌡️ Temperature: **{temp}°C** (feels like {feels}°C)",
            f"💧 Humidity: {hum}%",
            f"💨 Wind: {wind} km/h {self._wind_dir(wdir)}",
        ]
        if prec > 0:
            lines.append(f"🌧️ Precipitation: {prec} mm")

        if include_forecast:
            daily = data.get("daily", {})
            dates = daily.get("time", [])
            codes = daily.get("weathercode", [])
            highs = daily.get("temperature_2m_max", [])
            lows  = daily.get("temperature_2m_min", [])
            rains = daily.get("precipitation_sum", [])

            lines.append("\n### 📅 7-Day Forecast")
            from datetime import datetime
            for i, ds in enumerate(dates[:7]):
                try:
                    dt   = datetime.strptime(ds, "%Y-%m-%d")
                    day  = ["Today","Tomorrow"][i] if i < 2 else dt.strftime("%A")
                    c2   = WMO_CODES.get(codes[i] if i < len(codes) else 0, "")
                    hi   = highs[i] if i < len(highs) else "?"
                    lo   = lows[i]  if i < len(lows) else "?"
                    rain = rains[i] if i < len(rains) else 0
                    rs   = f" 🌧️{rain}mm" if rain and float(rain) > 0.5 else ""
                    lines.append(f"**{day}** — {c2} | ↑{hi}°C ↓{lo}°C{rs}")
                except Exception:
                    pass

        lines.append("\n_Live data via Open-Meteo_")
        return "\n".join(lines)

    # ── wttr.in fallback path ─────────────────────────────────────────────────

    async def _try_wttr(self, city: str, forecast: bool) -> Optional[str]:
        encoded = quote(city.replace(" ", "+"))
        url     = WTTR_URL.format(city=encoded)
        try:
            async with httpx.AsyncClient(
                timeout=10.0,
                headers={"User-Agent": "AetherAI/1.0 curl/7.68.0"},
                follow_redirects=True,
            ) as client:
                r = await client.get(url)
                r.raise_for_status()
                data = r.json()
                return self._format_wttr(city, data, forecast)
        except Exception as e:
            logger.warning(f"[WeatherAgent] wttr.in failed for '{city}': {e}")
            return None

    def _format_wttr(self, city: str, data: dict, include_forecast: bool) -> str:
        try:
            cur  = data["current_condition"][0]
            temp = cur.get("temp_C", "?")
            feels = cur.get("FeelsLikeC", "?")
            hum  = cur.get("humidity", "?")
            wind = cur.get("windspeedKmph", "?")
            wdir = cur.get("winddir16Point", "")
            prec = cur.get("precipMM", "0")
            vis  = cur.get("visibility", "")
            desc_list = cur.get("weatherDesc", [{}])
            desc = desc_list[0].get("value", "Unknown") if desc_list else "Unknown"
            code = cur.get("weatherCode", "113")
            cond = WTTR_CODES.get(str(code), desc)

            lines = [
                f"## 🌍 Weather — {city}",
                f"**{cond}**",
                f"🌡️ Temperature: **{temp}°C** (feels like {feels}°C)",
                f"💧 Humidity: {hum}%",
                f"💨 Wind: {wind} km/h {wdir}",
            ]
            if prec and float(prec) > 0:
                lines.append(f"🌧️ Precipitation: {prec} mm")
            if vis:
                lines.append(f"👁️ Visibility: {vis} km")

            if include_forecast:
                lines.append("\n### 📅 Forecast")
                weather_days = data.get("weather", [])
                for i, day_data in enumerate(weather_days[:4]):
                    date   = day_data.get("date", "")
                    max_c  = day_data.get("maxtempC", "?")
                    min_c  = day_data.get("mintempC", "?")
                    # Get midday hourly for condition
                    hourly = day_data.get("hourly", [])
                    cond2  = "—"
                    rain2  = "0"
                    if hourly:
                        mid = hourly[len(hourly)//2]
                        desc2 = mid.get("weatherDesc", [{}])
                        cond2 = desc2[0].get("value", "—") if desc2 else "—"
                        rain2 = mid.get("precipMM", "0")
                    day_label = ["Today","Tomorrow","Day 3","Day 4"][i] if i < 4 else date
                    rs = f" 🌧️{rain2}mm" if float(rain2) > 0.5 else ""
                    lines.append(f"**{day_label}** — {cond2} | ↑{max_c}°C ↓{min_c}°C{rs}")

            lines.append("\n_Live data via wttr.in_")
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"[WeatherAgent] wttr.in format error: {e}")
            return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _extract_city(self, query: str) -> Optional[str]:
        import re
        patterns = [
            r"weather\s+in\s+(.+?)(?:\s+today|\s+tomorrow|\s+this\s+week|\s+forecast|$)",
            r"(?:temperature|forecast|rain|raining)\s+in\s+(.+?)(?:\s+today|\s+tomorrow|$)",
            r"will\s+it\s+rain\s+in\s+(.+?)(?:\s+today|\s+tomorrow|$)",
            r"(?:weather|forecast)\s+(?:for|of)\s+(.+?)(?:\s+today|\s+this\s+week|$)",
            r"how\s+(?:hot|cold|warm)\s+is\s+(.+?)(?:\s+today|$)",
        ]
        for pat in patterns:
            m = re.search(pat, query.lower())
            if m:
                city = m.group(1).strip().title()
                if city and len(city) > 1 and city.lower() not in ("it","there","outside"):
                    return city
        return None

    def _wants_forecast(self, query: str) -> bool:
        kw = ["forecast","week","7 day","seven day","tomorrow","monday","tuesday",
              "wednesday","thursday","friday","saturday","sunday","next few days"]
        return any(k in query.lower() for k in kw)

    def _wind_dir(self, degrees) -> str:
        if not degrees: return ""
        try:
            dirs = ["N","NE","E","SE","S","SW","W","NW"]
            return dirs[round(float(degrees) / 45) % 8]
        except Exception:
            return ""
