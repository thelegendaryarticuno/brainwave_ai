"""SOP load, validate aur match karne ka kaam. Isme koi LLM nahi hai, sab code se decide hota hai."""
from __future__ import annotations

import operator
import os
from pathlib import Path

import yaml

SEVERITY = {"info": 0, "low": 1, "moderate": 2, "high": 3, "critical": 4}
SOURCES = {"hourly", "daily", "current"}
AGGS = {"max", "min", "mean", "sum"}
OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le,
       "==": operator.eq, "!=": operator.ne}
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "policies" / "sops.yaml"


class SopError(ValueError):
    pass


# ---------------- load aur validate
def _check_leaf(c: dict, where: str) -> None:
    if "metric" not in c or "op" not in c or "value" not in c:
        raise SopError(f"{where}: condition needs metric, op, value: {c}")
    if c.get("source", "hourly") not in SOURCES:
        raise SopError(f"{where}: bad source {c.get('source')}")
    if c.get("agg", "max") not in AGGS:
        raise SopError(f"{where}: bad agg {c.get('agg')}")
    if c["op"] not in OPS and c["op"] not in ("in", "between"):
        raise SopError(f"{where}: bad op {c['op']}")
    if c["op"] in ("in", "between") and not isinstance(c["value"], list):
        raise SopError(f"{where}: op {c['op']} needs a list value")
    h = c.get("hours")
    if h is not None and not (isinstance(h, list) and len(h) == 2):
        raise SopError(f"{where}: hours must be [start, end]")


def _check_tree(node: dict, where: str) -> None:
    if "all" in node or "any" in node:
        key = "all" if "all" in node else "any"
        if not isinstance(node[key], list) or not node[key]:
            raise SopError(f"{where}: '{key}' needs a non-empty list")
        for i, child in enumerate(node[key]):
            _check_tree(child, f"{where}.{key}[{i}]")
    else:
        _check_leaf(node, where)


def validate(doc: dict) -> None:
    topics, sops = doc.get("topics"), doc.get("sops")
    if not isinstance(topics, dict) or not isinstance(sops, list):
        raise SopError("file needs 'topics' (mapping) and 'sops' (list)")
    for t, spec in topics.items():
        if spec.get("parent") and spec["parent"] not in topics:
            raise SopError(f"topic {t}: unknown parent {spec['parent']}")
    ids = [s.get("id") for s in sops]
    if len(ids) != len(set(ids)):
        raise SopError("duplicate SOP ids")
    for s in sops:
        w = s.get("id", "?")
        for key in ("id", "category", "title", "applies_to"):
            if not s.get(key):
                raise SopError(f"{w}: missing '{key}'")
        if ("when" in s) == ("scored" in s):
            raise SopError(f"{w}: exactly one of 'when' / 'scored' required")
        for t in s["applies_to"]:
            if t != "*" and t not in topics:
                raise SopError(f"{w}: applies_to unknown topic '{t}'")
        for other in s.get("supersedes", []):
            if other not in ids:
                raise SopError(f"{w}: supersedes unknown SOP '{other}'")
        if "when" in s:
            if s.get("severity") not in SEVERITY:
                raise SopError(f"{w}: severity must be one of {list(SEVERITY)}")
            if not s.get("guidance"):
                raise SopError(f"{w}: missing guidance")
            _check_tree(s["when"], w)
        else:
            sc = s["scored"]
            if not sc.get("factors") or not sc.get("bands"):
                raise SopError(f"{w}: scored needs factors and bands")
            for f in sc["factors"]:
                if not {"metric", "ideal", "tolerable", "weight"} <= f.keys():
                    raise SopError(f"{w}: factor needs metric, ideal, tolerable, weight")
            for b in sc["bands"]:
                if b.get("severity") not in SEVERITY or "min_score" not in b or not b.get("guidance"):
                    raise SopError(f"{w}: band needs min_score, severity, guidance")


def _leaves(node: dict):
    if "all" in node or "any" in node:
        for child in node.get("all", node.get("any")):
            yield from _leaves(child)
    else:
        yield node


class SopStore:
    """YAML load karta hai aur file badalte hi dobara load kar leta hai. Galat edit hua to pichhla sahi version chalta rahega."""

    def __init__(self, path: str | os.PathLike = DEFAULT_PATH):
        self.path = Path(path)
        self._mtime = None
        self.doc: dict = {}
        self.error: str | None = None
        self.refresh()
        if not self.doc:
            raise SopError(self.error or "no SOPs loaded")

    def refresh(self) -> None:
        try:
            m = self.path.stat().st_mtime_ns
            if m == self._mtime:
                return
            doc = yaml.safe_load(self.path.read_text(encoding="utf-8"))
            validate(doc)
            self.doc, self._mtime, self.error = doc, m, None
        except Exception as e:  # pichhla sahi policy set hi chalne do
            self.error = f"{type(e).__name__}: {e}"
            self._mtime = None if not self.doc else self._mtime

    @property
    def topics(self) -> dict:
        self.refresh()
        return self.doc["topics"]

    @property
    def sops(self) -> list[dict]:
        self.refresh()
        return self.doc["sops"]

    def by_id(self, sid: str) -> dict | None:
        return next((s for s in self.sops if s["id"] == sid), None)

    def required_vars(self) -> dict[str, set[str]]:
        """SOPs ko jo Open-Meteo variables chahiye unki list, taaki naya SOP aane par fetcher na badalna pade."""
        need: dict[str, set[str]] = {"hourly": set(), "daily": set(), "current": set()}
        for s in self.sops:
            if "when" in s:
                for c in _leaves(s["when"]):
                    need[c.get("source", "hourly")].add(c["metric"])
            else:
                for f in s["scored"]["factors"]:
                    need[f.get("source", "hourly")].add(f["metric"])
        return need

    def ancestors(self, topic: str) -> set[str]:
        out, t = set(), topic
        while t and t not in out:
            out.add(t)
            t = self.topics.get(t, {}).get("parent")
        return out


# ---------------- evaluation
def _hour(ts: str) -> int:
    return int(ts[11:13])


def _series(snap: dict, src: str, metric: str, window: dict, hours: list | None):
    """Window ke liye `metric` ki values; data na ho to [] wapas."""
    day = window["day"]
    if src == "current":
        v = snap["current"].get(metric)
        return [] if window["day"] != snap["current"]["time"][:10] or v is None else [v]
    if src == "daily":
        data, times = snap["daily"], snap["daily_times"]
        if metric not in data or day not in times:
            return []
        v = data[metric][times.index(day)]
        return [] if v is None else [v]
    data = snap["hourly"]
    if metric not in data:
        return []
    lo, hi = window["hours"]
    if hours:
        lo, hi = max(lo, hours[0]), min(hi, hours[1])  # SOP ke hours aur user ki window ka common hissa lo
    return [v for t, v in zip(snap["hourly_times"], data[metric])
            if t[:10] == day and lo <= _hour(t) <= hi and v is not None]


def _agg(vals: list, how: str):
    return {"max": max, "min": min, "sum": sum, "mean": lambda x: sum(x) / len(x)}[how](vals)


def _eff_hours(hours, window):
    lo, hi = window["hours"]
    return [max(lo, hours[0]), min(hi, hours[1])] if hours else [lo, hi]


def _eval_leaf(c: dict, snap: dict, window: dict):
    src, how, op = c.get("source", "hourly"), c.get("agg", "max"), c["op"]
    vals = _series(snap, src, c["metric"], window, c.get("hours"))
    if not vals:
        return False, []
    if op == "in":  # list me hai ya nahi: window me koi bhi value mil jaye (jaise WMO weather code)
        hit = sorted({v for v in vals if v in c["value"]})
        ok, shown = bool(hit), hit
    else:
        shown = _agg(vals, how)
        ok = (c["value"][0] <= shown <= c["value"][1]) if op == "between" else OPS[op](shown, c["value"])
    if not ok:
        return False, []
    ev = {"metric": c["metric"], "source": src, "agg": "any-of" if op == "in" else how,
          "value": shown, "op": op, "threshold": c["value"],
          "hours": _eff_hours(c.get("hours"), window) if src == "hourly" else None}
    return True, [ev]


def _eval_tree(node: dict, snap: dict, window: dict):
    if "all" in node:
        evs = []
        for child in node["all"]:
            ok, e = _eval_tree(child, snap, window)
            if not ok:
                return False, []
            evs += e
        return True, evs
    if "any" in node:
        evs, hit = [], False
        for child in node["any"]:
            ok, e = _eval_tree(child, snap, window)
            if ok:
                hit, evs = True, evs + e
        return hit, evs
    return _eval_leaf(node, snap, window)


def _factor_score(v: float, f: dict) -> float:
    if f["ideal"][0] <= v <= f["ideal"][1]:
        return 1.0
    if f["tolerable"][0] <= v <= f["tolerable"][1]:
        return 0.5
    return 0.0


def _eval_scored(sc: dict, snap: dict, window: dict):
    total = wsum = 0.0
    evs = []
    for f in sc["factors"]:
        vals = _series(snap, f.get("source", "hourly"), f["metric"], window, f.get("hours"))
        if not vals:
            continue
        v = _agg(vals, f.get("agg", "max"))
        total += _factor_score(v, f) * f["weight"]
        wsum += f["weight"]
        evs.append({"metric": f["metric"], "source": f.get("source", "hourly"), "agg": f.get("agg", "max"),
                    "value": v, "op": "scored", "threshold": f["ideal"], "hours": window["hours"]})
    if not wsum:
        return None, []
    score = round(total / wsum, 2)
    band = next(b for b in sorted(sc["bands"], key=lambda b: -b["min_score"]) if score >= b["min_score"])
    return {"score": score, **band}, evs


def evaluate_all(store: SopStore, snap: dict, window: dict, topics: list[str]) -> list[dict]:
    """Jitne bhi SOP (topics, weather, window) par lagte hain sab wapas karta hai, sort nahi karta."""
    relevant = set()
    for t in topics:
        relevant |= store.ancestors(t)
    out = []
    for s in store.sops:
        if "*" not in s["applies_to"] and not (set(s["applies_to"]) & relevant):
            continue
        if "when" in s:
            ok, evs = _eval_tree(s["when"], snap, window)
            if ok:
                out.append(_match(s, s["severity"], s["guidance"], evs))
        else:
            res, evs = _eval_scored(s["scored"], snap, window)
            if res:
                m = _match(s, res["severity"], res["guidance"], evs)
                m["score"], m["label"] = res["score"], res["label"]
                out.append(m)
    return out


def _match(s: dict, severity: str, guidance: str, evidence: list) -> dict:
    return {"id": s["id"], "title": s["title"], "category": s["category"], "severity": severity,
            "guidance": " ".join(guidance.split()), "lead_with": bool(s.get("lead_with")),
            "supersedes": s.get("supersedes", []), "evidence": evidence}


def resolve(matches: list[dict], max_secondary: int = 2) -> dict:
    """Conflict ka rule (soch samajh kar rakha hai, README me bhi hai):
    1. jo SOP kisi doosre matched SOP se supersede ho gaya use hata do
    2. 'info' (all-clear) SOP tabhi dikhao jab low ya usse upar kuch na laga ho
    3. severity se sort karo, fir lead_with, fir file ka order
    4. primary pura batao aur `max_secondary` tak aur SOP "also applies" me dikhao (doosra khatra chhupana lamba jawab dene se bura hai)"""
    dropped = {o for m in matches for o in m["supersedes"]}
    live = [m for m in matches if m["id"] not in dropped]
    if any(SEVERITY[m["severity"]] >= 1 for m in live):
        live = [m for m in live if SEVERITY[m["severity"]] >= 1]
    order = {m["id"]: i for i, m in enumerate(matches)}
    live.sort(key=lambda m: (-SEVERITY[m["severity"]], not m["lead_with"], order[m["id"]]))
    shown = live[:1 + max_secondary]
    return {"primary": shown[0] if shown else None, "secondary": shown[1:],
            "suppressed": [m["id"] for m in matches if m not in shown]}
