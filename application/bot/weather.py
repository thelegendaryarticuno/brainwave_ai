"""Open-Meteo se location aur forecast lana. Bot jo bhi number bolta hai wo yahin se aata hai."""
from __future__ import annotations

import httpx

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

BASE = {
    "current": {"temperature_2m", "apparent_temperature", "wind_speed_10m", "wind_gusts_10m",
                "precipitation", "weather_code"},
    "hourly": {"temperature_2m", "apparent_temperature", "precipitation", "precipitation_probability",
               "wind_speed_10m", "wind_gusts_10m", "uv_index", "weather_code", "visibility"},
    "daily": {"precipitation_sum", "temperature_2m_max", "temperature_2m_min", "wind_gusts_10m_max"},
}

WMO = {0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "rime fog",
       51: "light drizzle", 53: "drizzle", 55: "dense drizzle", 61: "slight rain", 63: "moderate rain",
       65: "heavy rain", 66: "freezing rain", 67: "heavy freezing rain", 71: "slight snow", 73: "snow",
       75: "heavy snow", 80: "light showers", 81: "showers", 82: "violent showers", 95: "thunderstorm",
       96: "thunderstorm with hail", 99: "thunderstorm with heavy hail"}


class WeatherError(Exception):
    """Weather ya location nahi mil paya. `kind` = 'location' ya 'weather'."""

    def __init__(self, kind: str, msg: str):
        super().__init__(msg)
        self.kind = kind


def geocode(name: str, country_hint: str | None = None, client: httpx.Client | None = None) -> dict:
    c = client or httpx.Client(timeout=10)
    try:
        r = c.get(GEO_URL, params={"name": name, "count": 10, "language": "en", "format": "json"})
        r.raise_for_status()
        results = r.json().get("results") or []
    except (httpx.HTTPError, ValueError) as e:
        raise WeatherError("location", f"geocoding failed: {e}") from e
    if not results:
        raise WeatherError("location", f"no place found for '{name}'")
    exact = [x for x in results if x["name"].lower() == name.strip().lower()] or results
    pick = exact[0]
    if country_hint:
        h = country_hint.lower()
        pick = next((x for x in exact if h in {str(x.get(k, "")).lower()
                                                 for k in ("country", "country_code", "admin1")}), results[0])
    distinct = {(x.get("country"), x.get("admin1")) for x in exact}
    label = ", ".join(p for p in (pick["name"], pick.get("admin1"), pick.get("country")) if p)
    return {"name": label, "latitude": pick["latitude"], "longitude": pick["longitude"],
            "ambiguous": len(distinct) > 1}


def fetch_forecast(loc: dict, need: dict[str, set[str]], client: httpx.Client | None = None) -> dict:
    """2 din ka current + hourly + daily laata hai. `need` = SOPs ko jo extra variables chahiye."""
    c = client or httpx.Client(timeout=15)

    def call(variables: dict[str, set[str]]):
        params = {"latitude": loc["latitude"], "longitude": loc["longitude"], "timezone": "auto",
                  "forecast_days": 2, "current": ",".join(sorted(variables["current"])),
                  "hourly": ",".join(sorted(variables["hourly"])), "daily": ",".join(sorted(variables["daily"]))}
        return c.get(FORECAST_URL, params=params)

    merged = {k: BASE[k] | need.get(k, set()) for k in BASE}
    try:
        r = call(merged)
        if r.status_code == 400:  # SOP me aisa variable hai jo Open-Meteo nahi jaanta: base variables se kaam chalao, crash mat karo
            r = call(BASE)
        r.raise_for_status()
        j = r.json()
        cur, hourly, daily = j["current"], j["hourly"], j["daily"]
    except (httpx.HTTPError, ValueError, KeyError) as e:
        raise WeatherError("weather", f"forecast unavailable: {type(e).__name__}: {e}") from e
    if cur.get("temperature_2m") is None:
        raise WeatherError("weather", "forecast response had no values")
    return {"location": loc["name"], "current": cur, "units": j.get("current_units", {}),
            "hourly_times": hourly.pop("time"), "hourly": hourly,
            "daily_times": daily.pop("time"), "daily": daily,
            "daily_units": j.get("daily_units", {}), "hourly_units": j.get("hourly_units", {})}


PARTS = {"morning": (5, 11), "afternoon": (12, 16), "evening": (17, 21), "night": (21, 23), "all_day": (0, 23)}


def make_window(snap: dict, day_offset: int, part: str) -> dict | None:
    """Sawaal kis din aur kin ghanton ka hai. Aaj ki wo window nikal chuki ho to None."""
    now = snap["current"]["time"]
    today, hour = now[:10], int(now[11:13])
    day = snap["daily_times"][min(day_offset, len(snap["daily_times"]) - 1)]
    if part == "now":
        lo, hi = hour, min(hour + 3, 23)
    else:
        lo, hi = PARTS.get(part, PARTS["all_day"])
    if day == today:
        lo = max(lo, hour)
    if lo > hi:
        return None
    return {"day": day, "hours": [lo, hi], "part": part, "is_today": day == today}
