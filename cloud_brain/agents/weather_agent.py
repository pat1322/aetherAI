"""
AetherAI — Weather Agent
Uses Open-Meteo (free, no API key) + Open-Meteo Geocoding API.

Capabilities:
  • Current weather (temperature, humidity, wind, condition)
  • 7-day forecast with daily highs/lows and precipitation
  • Any city in the world by name
  • Philippine cities default to Asia/Manila timezone

Trigger examples:
  "what's the weather in Manila"
  "will it rain in Cebu tomorrow"
  "forecast for Tokyo this week"
  "temperature in New York"
  "what's the weather today"
"""

import asyncio
import logging
from typing import Optional

import httpx
from agents import BaseAgent

logger = logging.getLogger(__name__)

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL   = "https://api.open-meteo.com/v1/forecast"

# WMO weather code descriptions
WMO_CODES = {
    0:  "Clear sky ☀️",
    1:  "Mainly clear 🌤️",  2: "Partly cloudy ⛅", 3: "Overcast ☁️",
    45: "Foggy 🌫️",         48: "Icy fog 🌫️",
    51: "Light drizzle 🌦️", 53: "Drizzle 🌦️",    55: "Heavy drizzle 🌦️",
    61: "Light rain 🌧️",    63: "Rain 🌧️",         65: "Heavy rain 🌧️",
    71: "Light snow 🌨️",    73: "Snow 🌨️",          75: "Heavy snow 🌨️",
    77: "Snow grains 🌨️",
    80: "Light showers 🌦️", 81: "Showers 🌦️",      82: "Violent showers ⛈️",
    85: "Snow showers 🌨️",  86: "Heavy snow showers 🌨️",
    95: "Thunderstorm ⛈️",  96: "Thunderstorm w/ hail ⛈️",
    99: "Thunderstorm w/ heavy hail ⛈️",
}

DEFAULT_CITY     = "Manila"
DEFAULT_TIMEZONE = "Asia/Manila"


class WeatherAgent(BaseAgent):
    name        = "weather_agent"
    description = "Real-time weather and forecasts for any city worldwide using Open-Meteo"

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

        logger.info(f"[WeatherAgent] city={city} forecast={forecast}")

        # Step 1: Geocode city → lat/lon
        lat, lon, full_city, timezone = await self._geocode(city)
        if lat is None:
            return f"⚠️ Could not find location: **{city}**. Try a more specific city name."

        # Step 2: Fetch weather
        weather = await self._fetch_weather(lat, lon, timezone, include_daily=forecast)
        if not weather:
            return f"⚠️ Could not fetch weather data for **{full_city}**."

        return self._format_response(full_city, weather, forecast, query)

    # ── Geocoding ──────────────────────────────────────────────────────────────

    async def _geocode(self, city: str) -> tuple:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(GEOCODING_URL, params={
                    "name": city, "count": 1, "language": "en", "format": "json"
                })
                r.raise_for_status()
                data = r.json()

            results = data.get("results", [])
            if not results:
                return None, None, city, DEFAULT_TIMEZONE

            loc      = results[0]
            lat      = loc["latitude"]
            lon      = loc["longitude"]
            name     = loc.get("name", city)
            country  = loc.get("country", "")
            timezone = loc.get("timezone", DEFAULT_TIMEZONE)
            full     = f"{name}, {country}" if country else name
            return lat, lon, full, timezone

        except Exception as e:
            logger.warning(f"[WeatherAgent] Geocoding failed for '{city}': {e}")
            return None, None, city, DEFAULT_TIMEZONE

    # ── Weather fetch ─────────────────────────────────────────────────────────

    async def _fetch_weather(self, lat: float, lon: float,
                              timezone: str, include_daily: bool = True) -> Optional[dict]:
        params = {
            "latitude":  lat,
            "longitude": lon,
            "timezone":  timezone,
            "current": ",".join([
                "temperature_2m", "apparent_temperature",
                "relative_humidity_2m", "weathercode",
                "windspeed_10m", "winddirection_10m",
                "precipitation", "cloudcover",
            ]),
        }
        if include_daily:
            params["daily"] = ",".join([
                "weathercode", "temperature_2m_max", "temperature_2m_min",
                "precipitation_sum", "windspeed_10m_max",
                "sunrise", "sunset",
            ])
            params["forecast_days"] = 7

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(WEATHER_URL, params=params)
                r.raise_for_status()
                return r.json()
        except Exception as e:
            logger.warning(f"[WeatherAgent] Weather fetch failed: {e}")
            return None

    # ── Formatting ────────────────────────────────────────────────────────────

    def _format_response(self, city: str, data: dict,
                          include_forecast: bool, query: str) -> str:
        cur = data.get("current", {})
        temp      = cur.get("temperature_2m", "?")
        feels     = cur.get("apparent_temperature", "?")
        humidity  = cur.get("relative_humidity_2m", "?")
        wind      = cur.get("windspeed_10m", "?")
        wind_dir  = cur.get("winddirection_10m", "?")
        precip    = cur.get("precipitation", 0)
        code      = cur.get("weathercode", 0)
        condition = WMO_CODES.get(code, "Unknown")

        lines = [
            f"## 🌍 Weather — {city}",
            f"",
            f"**{condition}**",
            f"🌡️  Temperature: **{temp}°C** (feels like {feels}°C)",
            f"💧  Humidity: {humidity}%",
            f"💨  Wind: {wind} km/h from {self._wind_dir(wind_dir)}",
        ]
        if precip and precip > 0:
            lines.append(f"🌧️  Precipitation: {precip} mm")

        if include_forecast:
            daily = data.get("daily", {})
            dates    = daily.get("time", [])
            codes    = daily.get("weathercode", [])
            highs    = daily.get("temperature_2m_max", [])
            lows     = daily.get("temperature_2m_min", [])
            rains    = daily.get("precipitation_sum", [])
            sunrises = daily.get("sunrise", [])
            sunsets  = daily.get("sunset", [])

            lines.append(f"\n### 📅 7-Day Forecast\n")
            day_names = ["Today", "Tomorrow", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            from datetime import datetime
            for i, date_str in enumerate(dates[:7]):
                try:
                    dt   = datetime.strptime(date_str, "%Y-%m-%d")
                    day  = day_names[i] if i < 2 else dt.strftime("%A")
                    cond = WMO_CODES.get(codes[i] if i < len(codes) else 0, "")
                    hi   = highs[i] if i < len(highs) else "?"
                    lo   = lows[i]  if i < len(lows)  else "?"
                    rain = rains[i] if i < len(rains) else 0
                    rain_str = f" 🌧️ {rain}mm" if rain and rain > 0.5 else ""

                    # Sunrise/sunset for today only
                    if i == 0 and sunrises and sunsets:
                        rise = sunrises[0].split("T")[-1][:5] if sunrises else ""
                        sset = sunsets[0].split("T")[-1][:5] if sunsets else ""
                        lines.append(
                            f"**{day}** — {cond} | ↑{hi}°C ↓{lo}°C{rain_str} "
                            f"| 🌅 {rise} 🌇 {sset}"
                        )
                    else:
                        lines.append(f"**{day}** — {cond} | ↑{hi}°C ↓{lo}°C{rain_str}")
                except Exception:
                    continue
        else:
            lines.append(f"\n_Say 'forecast for {city}' for a 7-day outlook._")

        lines.append(f"\n_Powered by Open-Meteo • Live data_")
        return "\n".join(lines)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _extract_city(self, query: str) -> Optional[str]:
        import re
        patterns = [
            r"weather\s+in\s+(.+?)(?:\s+today|\s+tomorrow|\s+this\s+week|$)",
            r"(?:temperature|forecast|rain|raining)\s+in\s+(.+?)(?:\s+today|\s+tomorrow|$)",
            r"will\s+it\s+rain\s+in\s+(.+?)(?:\s+today|\s+tomorrow|$)",
            r"(?:weather|forecast)\s+(?:for|of)\s+(.+?)(?:\s+today|\s+this\s+week|$)",
        ]
        for pat in patterns:
            m = re.search(pat, query.lower())
            if m:
                city = m.group(1).strip().title()
                if city and len(city) > 1:
                    return city
        return None

    def _wants_forecast(self, query: str) -> bool:
        kw = ["forecast", "week", "7 day", "seven day", "tomorrow",
              "monday", "tuesday", "wednesday", "thursday", "friday",
              "saturday", "sunday", "next few days"]
        ql = query.lower()
        return any(k in ql for k in kw)

    def _wind_dir(self, degrees: float) -> str:
        if degrees is None: return "?"
        dirs = ["N","NE","E","SE","S","SW","W","NW"]
        return dirs[round(degrees / 45) % 8]
