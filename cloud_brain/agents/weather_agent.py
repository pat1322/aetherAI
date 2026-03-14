"""
AetherAI — Weather Agent  (patch 5)

Fix: Added detailed error logging, timeout handling per API call,
and hardcoded fallback coordinates for common Philippine cities
so weather still works even if geocoding API is slow or unreachable.
"""

import asyncio
import logging
from typing import Optional

import httpx
from agents import BaseAgent

logger = logging.getLogger(__name__)

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL   = "https://api.open-meteo.com/v1/forecast"

WMO_CODES = {
    0:  "Clear sky ☀️",   1: "Mainly clear 🌤️",  2: "Partly cloudy ⛅",  3: "Overcast ☁️",
    45: "Foggy 🌫️",       48: "Icy fog 🌫️",
    51: "Light drizzle 🌦️", 53: "Drizzle 🌦️",   55: "Heavy drizzle 🌦️",
    61: "Light rain 🌧️",  63: "Rain 🌧️",          65: "Heavy rain 🌧️",
    71: "Light snow 🌨️",  73: "Snow 🌨️",           75: "Heavy snow 🌨️",
    77: "Snow grains 🌨️",
    80: "Light showers 🌦️", 81: "Showers 🌦️",    82: "Violent showers ⛈️",
    95: "Thunderstorm ⛈️",  96: "Thunderstorm w/ hail ⛈️",
    99: "Thunderstorm w/ heavy hail ⛈️",
}

DEFAULT_CITY     = "Manila"
DEFAULT_TIMEZONE = "Asia/Manila"

# Hardcoded fallback coords for common PH cities (in case geocoding API is slow)
PH_CITY_FALLBACK = {
    "manila":   (14.5995, 120.9842, "Manila, Philippines",       "Asia/Manila"),
    "cebu":     (10.3157, 123.8854, "Cebu, Philippines",         "Asia/Manila"),
    "davao":    (7.1907,  125.4553, "Davao, Philippines",        "Asia/Manila"),
    "quezon":   (14.6760, 121.0437, "Quezon City, Philippines",  "Asia/Manila"),
    "makati":   (14.5547, 121.0244, "Makati, Philippines",       "Asia/Manila"),
    "pasig":    (14.5764, 121.0851, "Pasig, Philippines",        "Asia/Manila"),
    "taguig":   (14.5176, 121.0509, "Taguig, Philippines",       "Asia/Manila"),
    "baguio":   (16.4023, 120.5960, "Baguio, Philippines",       "Asia/Manila"),
    "iloilo":   (10.7202, 122.5621, "Iloilo, Philippines",       "Asia/Manila"),
    "cagayan":  (8.4542,  124.6319, "Cagayan de Oro, Philippines","Asia/Manila"),
}


class WeatherAgent(BaseAgent):
    name        = "weather_agent"
    description = "Real-time weather and forecasts for any city using Open-Meteo"

    async def run(self, parameters: dict, task_id: str, context: str = "") -> Optional[str]:
        try:
            return await self._run(parameters, task_id, context)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[WeatherAgent] Unexpected error: {e}", exc_info=True)
            return f"⚠️ WeatherAgent error: {e}"

    async def _run(self, parameters: dict, task_id: str, context: str) -> Optional[str]:
        query    = parameters.get("query") or context or ""
        city     = parameters.get("city") or self._extract_city(query) or DEFAULT_CITY
        forecast = parameters.get("forecast", False) or self._wants_forecast(query)

        logger.info(f"[WeatherAgent] city='{city}' forecast={forecast}")

        # Step 1: Get coordinates
        lat, lon, full_city, timezone = await self._geocode(city)
        if lat is None:
            return (
                f"⚠️ Could not find location: **{city}**\n"
                f"Try being more specific, e.g. 'weather in Manila Philippines' "
                f"or 'weather in Cebu City'."
            )

        logger.info(f"[WeatherAgent] Geocoded '{city}' → {lat},{lon} ({full_city})")

        # Step 2: Fetch weather data
        weather = await self._fetch_weather(lat, lon, timezone, include_daily=forecast)
        if not weather:
            return (
                f"⚠️ Could not fetch weather data for **{full_city}**.\n"
                f"Open-Meteo may be temporarily unavailable. Please try again in a moment."
            )

        return self._format_response(full_city, weather, forecast, city)

    # ── Geocoding ──────────────────────────────────────────────────────────────

    async def _geocode(self, city: str) -> tuple:
        city_lc = city.lower().strip()

        # Check PH fallback first (instant, no network needed)
        for key, (lat, lon, full, tz) in PH_CITY_FALLBACK.items():
            if key in city_lc:
                logger.info(f"[WeatherAgent] Using PH fallback for '{city}'")
                return lat, lon, full, tz

        # Try geocoding API
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                logger.info(f"[WeatherAgent] Geocoding '{city}'...")
                r = await client.get(GEOCODING_URL, params={
                    "name":     city,
                    "count":    1,
                    "language": "en",
                    "format":   "json",
                })
                r.raise_for_status()
                data = r.json()
                logger.info(f"[WeatherAgent] Geocoding response: {data}")

            results = data.get("results", [])
            if not results:
                logger.warning(f"[WeatherAgent] No geocoding results for '{city}'")
                return None, None, city, DEFAULT_TIMEZONE

            loc      = results[0]
            lat      = loc["latitude"]
            lon      = loc["longitude"]
            name     = loc.get("name", city)
            country  = loc.get("country", "")
            timezone = loc.get("timezone", DEFAULT_TIMEZONE)
            full     = f"{name}, {country}" if country else name
            return lat, lon, full, timezone

        except httpx.TimeoutException:
            logger.warning(f"[WeatherAgent] Geocoding timed out for '{city}'")
            # If city contains a known PH keyword, use Manila as fallback
            if any(w in city_lc for w in ["phil", "metro", "ncr"]):
                return PH_CITY_FALLBACK["manila"]
            return None, None, city, DEFAULT_TIMEZONE

        except Exception as e:
            logger.error(f"[WeatherAgent] Geocoding failed for '{city}': {e}")
            return None, None, city, DEFAULT_TIMEZONE

    # ── Weather fetch ──────────────────────────────────────────────────────────

    async def _fetch_weather(self, lat: float, lon: float,
                              timezone: str, include_daily: bool = True) -> Optional[dict]:
        params = {
            "latitude":  lat,
            "longitude": lon,
            "timezone":  timezone,
            "current":   ",".join([
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
            async with httpx.AsyncClient(timeout=12.0) as client:
                logger.info(f"[WeatherAgent] Fetching weather lat={lat} lon={lon} tz={timezone}")
                r = await client.get(WEATHER_URL, params=params)
                logger.info(f"[WeatherAgent] Weather API status: {r.status_code}")
                r.raise_for_status()
                data = r.json()
                logger.info(f"[WeatherAgent] Weather data keys: {list(data.keys())}")
                return data

        except httpx.TimeoutException:
            logger.error(f"[WeatherAgent] Weather API timed out")
            return None
        except Exception as e:
            logger.error(f"[WeatherAgent] Weather fetch failed: {e}", exc_info=True)
            return None

    # ── Formatting ─────────────────────────────────────────────────────────────

    def _format_response(self, city: str, data: dict,
                          include_forecast: bool, original_query: str) -> str:
        cur = data.get("current", {})
        if not cur:
            return f"⚠️ Weather data received but no current conditions available for **{city}**."

        temp      = cur.get("temperature_2m", "?")
        feels     = cur.get("apparent_temperature", "?")
        humidity  = cur.get("relative_humidity_2m", "?")
        wind      = cur.get("windspeed_10m", "?")
        wind_dir  = cur.get("winddirection_10m", 0)
        precip    = cur.get("precipitation", 0) or 0
        code      = cur.get("weathercode", 0)
        condition = WMO_CODES.get(code, "Unknown")

        lines = [
            f"## 🌍 Weather — {city}",
            f"",
            f"**{condition}**",
            f"🌡️  Temperature: **{temp}°C** (feels like {feels}°C)",
            f"💧  Humidity: {humidity}%",
            f"💨  Wind: {wind} km/h {self._wind_dir(wind_dir)}",
        ]
        if precip > 0:
            lines.append(f"🌧️  Precipitation: {precip} mm")

        if include_forecast:
            daily    = data.get("daily", {})
            dates    = daily.get("time", [])
            codes    = daily.get("weathercode", [])
            highs    = daily.get("temperature_2m_max", [])
            lows     = daily.get("temperature_2m_min", [])
            rains    = daily.get("precipitation_sum", [])
            sunrises = daily.get("sunrise", [])
            sunsets  = daily.get("sunset", [])

            lines.append(f"\n### 📅 7-Day Forecast\n")
            from datetime import datetime
            for i, date_str in enumerate(dates[:7]):
                try:
                    dt      = datetime.strptime(date_str, "%Y-%m-%d")
                    day     = ["Today","Tomorrow","Mon","Tue","Wed","Thu","Fri","Sat","Sun"][i] \
                              if i < 2 else dt.strftime("%A")
                    cond    = WMO_CODES.get(codes[i] if i < len(codes) else 0, "")
                    hi      = highs[i] if i < len(highs) else "?"
                    lo      = lows[i]  if i < len(lows)  else "?"
                    rain    = rains[i] if i < len(rains)  else 0
                    rain_s  = f" 🌧️ {rain}mm" if rain and float(rain) > 0.5 else ""

                    if i == 0 and sunrises and sunsets:
                        rise = sunrises[0].split("T")[-1][:5] if sunrises else ""
                        sset = sunsets[0].split("T")[-1][:5] if sunsets else ""
                        lines.append(
                            f"**{day}** — {cond} | ↑{hi}°C ↓{lo}°C{rain_s} "
                            f"| 🌅{rise} 🌇{sset}"
                        )
                    else:
                        lines.append(f"**{day}** — {cond} | ↑{hi}°C ↓{lo}°C{rain_s}")
                except Exception:
                    continue
        else:
            lines.append(f"\n_Tip: Say 'forecast for {city}' for a 7-day outlook._")

        lines.append(f"\n_Live data via Open-Meteo_")
        return "\n".join(lines)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _extract_city(self, query: str) -> Optional[str]:
        import re
        patterns = [
            r"weather\s+in\s+(.+?)(?:\s+today|\s+tomorrow|\s+this\s+week|\s+forecast|$)",
            r"(?:temperature|forecast|rain|raining)\s+in\s+(.+?)(?:\s+today|\s+tomorrow|$)",
            r"will\s+it\s+rain\s+in\s+(.+?)(?:\s+today|\s+tomorrow|$)",
            r"(?:weather|forecast)\s+(?:for|of)\s+(.+?)(?:\s+today|\s+this\s+week|$)",
            r"how\s+(?:hot|cold|warm)\s+is\s+(.+?)(?:\s+today|$)",
            r"(?:climate|temperature)\s+in\s+(.+?)(?:\s+today|$)",
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
              "wednesday","thursday","friday","saturday","sunday","next few days","outlook"]
        return any(k in query.lower() for k in kw)

    def _wind_dir(self, degrees) -> str:
        if degrees is None: return ""
        try:
            dirs = ["N","NE","E","SE","S","SW","W","NW"]
            return dirs[round(float(degrees) / 45) % 8]
        except Exception:
            return ""
