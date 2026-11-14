"""Fixtures shared by offline tests (no network, no model)."""
from bot.graph import Deps, build_graph, chat
from bot.sops import SopStore
from bot.weather import WeatherError

DAY, DAY2 = "2026-09-04", "2026-09-05"
UNITS = {"temperature_2m": "°C", "apparent_temperature": "°C", "wind_speed_10m": "km/h", "wind_gusts_10m": "km/h",
         "precipitation": "mm", "precipitation_probability": "%", "uv_index": "", "visibility": "m",
         "weather_code": "wmo code", "precipitation_sum": "mm"}
CALM = {"temperature_2m": 24, "apparent_temperature": 25, "precipitation": 0.0, "precipitation_probability": 5,
        "wind_speed_10m": 8, "wind_gusts_10m": 12, "uv_index": 2, "visibility": 24000, "weather_code": 1}


def snapshot(hourly=None, daily=None, now_hour=8, day_two=None):
    """hourly: {metric: const or {hour: value}} applied to DAY; day_two likewise for DAY2."""
    def col(spec, base):
        return [spec.get(h, base) if isinstance(spec, dict) else spec for h in range(24)]
    hourly, day_two = hourly or {}, day_two or {}
    h = {m: col(hourly.get(m, b), b) + col(day_two.get(m, b), b) for m, b in CALM.items()}
    for m, spec in {**hourly, **day_two}.items():  # jin variables ka base column nahi hai unke liye
        if m not in h:
            h[m] = col(hourly.get(m, 0), 0) + col(day_two.get(m, 0), 0)
    d = {"precipitation_sum": [0.0, 0.0], "temperature_2m_max": [30, 30], "temperature_2m_min": [20, 20],
         "wind_gusts_10m_max": [12, 12]}
    d.update(daily or {})
    cur = {"time": f"{DAY}T{now_hour:02d}:00", **CALM}
    for m, spec in hourly.items():
        if not isinstance(spec, dict) and m in cur:
            cur[m] = spec
    return {"location": "Testville, Teststate, Testland", "current": cur, "units": UNITS,
            "hourly_times": [f"{DAY}T{i:02d}:00" for i in range(24)] + [f"{DAY2}T{i:02d}:00" for i in range(24)],
            "hourly": h, "hourly_units": UNITS, "daily_times": [DAY, DAY2], "daily": d, "daily_units": UNITS}


class FakeLLM:
    """json(): scripted parse (dict, exception, or callable of the user text).
    text(): scripted drafts, else echoes the GUIDANCE lines it was given."""

    def __init__(self, parse, drafts=None):
        self.parse, self.drafts, self.compose_calls, self.seen_user = parse, list(drafts or []), 0, []

    def json(self, system, user):
        self.seen_user.append(user)
        p = self.parse(user) if callable(self.parse) else self.parse
        if isinstance(p, Exception):
            raise p
        return p

    def text(self, system, user):
        self.compose_calls += 1
        self.seen_user.append(user)
        if self.drafts:
            return self.drafts.pop(0)
        return " ".join(l[len("GUIDANCE: "):] for l in user.splitlines() if l.startswith("GUIDANCE: "))


def parse(topics=("cycling",), location="Testville", day_offset=None, part=None, intent="advice", activity="biking"):
    return {"intent": intent, "topics": list(topics), "activity_text": activity, "location": location,
            "country_hint": None, "day_offset": day_offset, "part": part}


def make_graph(llm, snap=None, store=None, geo_fail=False, wx_fail=False):
    def geocode(name, hint=None):
        if geo_fail:
            raise WeatherError("location", "no place found")
        return {"name": name, "latitude": 1.0, "longitude": 2.0, "ambiguous": False}

    def forecast(loc, need):
        if wx_fail:
            raise WeatherError("weather", "timeout")
        return snap
    return build_graph(Deps(store=store or SopStore(), llm=llm, geocode=geocode, forecast=forecast))


def ask(g, msg, sid="s1"):
    return chat(g, sid, msg)
