import os
import textwrap
import time

import pytest
import yaml

from bot.sops import SopError, SopStore, evaluate_all, resolve, validate
from bot.weather import make_window
from tests.helpers import snapshot

store = SopStore()


def win(snap, part="all_day", off=0):
    return make_window(snap, off, part)


def ids(snap, topics, part="all_day", off=0):
    return {m["id"] for m in evaluate_all(store, snap, win(snap, part, off), topics)}


def bump(p):  # mtime badal do taaki hot-reload edit pakad le
    os.utime(p, (time.time() + 5, time.time() + 5))


def test_policy_set_meets_brief():
    sops = store.sops
    assert len(sops) >= 10
    assert len({s["category"] for s in sops}) >= 3
    sev = {s["severity"] for s in sops if "severity" in s} | \
          {b["severity"] for s in sops if "scored" in s for b in s["scored"]["bands"]}
    assert len(sev) >= 4
    assert any("scored" in s for s in sops)  # fuzzy wala scenario hai


def test_uv_sop_only_inside_its_hours():
    s = snapshot(hourly={"uv_index": {12: 9}})
    assert "SOP-EX-UV-01" in ids(s, ["running"], "all_day")
    assert "SOP-EX-UV-01" not in ids(s, ["running"], "evening")  # window 11-16 se match nahi karti


def test_cycling_wind_is_a_safety_sop_and_scoped_to_topic():
    s = snapshot(hourly={"wind_speed_10m": 45})
    assert "SOP-CYC-WIND-01" in ids(s, ["cycling"])
    assert "SOP-CYC-WIND-01" in ids(s, ["two_wheeler"])
    assert "SOP-CYC-WIND-01" not in ids(s, ["picnic"])


def test_parent_topic_inheritance():
    s = snapshot(hourly={"apparent_temperature": 42})
    assert "SOP-EX-HEAT-01" in ids(s, ["hiking"])  # hiking -> outdoor_exercise


def test_rain_system_fires_on_combination_not_single_threshold():
    # sirf 40 mm 64.5 IMD line se kam hai aur sirf 50 ke gusts halke hain; dono saath ho to system hai.
    s = snapshot(hourly={"wind_gusts_10m": {15: 50}}, daily={"precipitation_sum": [40.0, 0]})
    assert "SOP-SEV-RAIN-SYSTEM-01" in ids(s, ["picnic"])
    assert "SOP-SEV-RAIN-SYSTEM-01" not in ids(snapshot(daily={"precipitation_sum": [40.0, 0]}), ["picnic"])
    assert "SOP-SEV-RAIN-SYSTEM-01" in ids(snapshot(daily={"precipitation_sum": [70.0, 0]}), ["pets"])  # kisi bhi category me


def test_rain_system_leads_and_supersedes_plain_rain_sops():
    s = snapshot(hourly={"wind_speed_10m": 45, "precipitation_probability": 90}, daily={"precipitation_sum": [80.0, 0]})
    r = resolve(evaluate_all(store, s, win(s), ["cycling"]))
    assert r["primary"]["id"] == "SOP-SEV-RAIN-SYSTEM-01"            # 'high' ka tie lead_with se tuta
    assert [m["id"] for m in r["secondary"]] == ["SOP-CYC-WIND-01"]  # doosra khatra bhi dikha
    assert "SOP-CYC-WET-01" in r["suppressed"]


def test_thunder_outranks_everything():
    s = snapshot(hourly={"weather_code": {16: 95}}, daily={"precipitation_sum": [80.0, 0]})
    assert resolve(evaluate_all(store, s, win(s), ["cycling"]))["primary"]["id"] == "SOP-SEV-THUNDER-01"


def test_all_clear_sop_hidden_when_a_warning_fires_and_shown_alone_otherwise():
    calm = snapshot()
    assert resolve(evaluate_all(store, calm, win(calm), ["running"]))["primary"]["id"] == "SOP-EX-OK-01"
    hot = snapshot(hourly={"uv_index": {12: 9}})
    assert resolve(evaluate_all(store, hot, win(hot), ["running"]))["primary"]["id"] == "SOP-EX-UV-01"


@pytest.mark.parametrize("hourly,label", [({}, "good"),
                                           ({"precipitation_probability": 30, "wind_speed_10m": 25}, "fair"),
                                           ({"precipitation_probability": 80, "wind_speed_10m": 35, "uv_index": 10}, "poor")])
def test_picnic_graded_bands(hourly, label):
    s = snapshot(hourly=hourly)
    m = [x for x in evaluate_all(store, s, win(s), ["picnic"]) if x["id"] == "SOP-LEISURE-PICNIC-01"][0]
    assert m["label"] == label


def test_no_topic_means_nothing_applies_in_calm_weather():
    assert ids(snapshot(), []) == set()


def test_missing_data_never_fires():
    s = snapshot()
    del s["hourly"]["uv_index"]
    assert "SOP-EX-UV-01" not in ids(s, ["running"])


def test_invalid_policy_rejected():
    doc = yaml.safe_load(open(store.path, encoding="utf-8"))
    doc["sops"][0]["severity"] = "apocalyptic"
    with pytest.raises(SopError):
        validate(doc)


def test_eleventh_sop_added_live_without_code_change(tmp_path):
    """The review-call scenario: append an SOP to the YAML, same process, no restart, a brand-new weather variable."""
    p = tmp_path / "sops.yaml"
    p.write_text(open(store.path, encoding="utf-8").read(), encoding="utf-8")
    st = SopStore(p)
    s = snapshot(hourly={"relative_humidity_2m": 95})
    assert "SOP-EX-HUMID-01" not in {m["id"] for m in evaluate_all(st, s, win(s), ["running"])}
    n = len(st.sops)
    p.write_text(p.read_text(encoding="utf-8") + textwrap.indent(textwrap.dedent("""
      - id: SOP-EX-HUMID-01
        category: outdoor_exercise
        title: Very humid
        severity: low
        applies_to: [running]
        when: {metric: relative_humidity_2m, op: ">=", value: 90}
        guidance: Humidity is very high; slow your pace and drink more water.
    """), "  "), encoding="utf-8")
    bump(p)
    assert len(st.sops) == n + 1
    assert "relative_humidity_2m" in st.required_vars()["hourly"]  # fetcher khud API se maang lega
    assert "SOP-EX-HUMID-01" in {m["id"] for m in evaluate_all(st, s, win(s), ["running"])}


def test_broken_edit_keeps_last_good_policy(tmp_path):
    p = tmp_path / "sops.yaml"
    p.write_text(open(store.path, encoding="utf-8").read(), encoding="utf-8")
    st = SopStore(p)
    n = len(st.sops)
    p.write_text("sops: [ {id: x} ]\ntopics: {}", encoding="utf-8")
    bump(p)
    assert len(st.sops) == n and st.error
