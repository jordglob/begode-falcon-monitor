"""Ride segmentation and statistics on synthetic data."""
import sqlite3

import pytest

from falcon import rides


def pts(t0, n, lat0=51.0, step_deg=0.0001, v=20.0, dt=2.0):
    return [{"ts": t0 + i * dt, "lat": lat0 + i * step_deg, "lon": 0.0, "speed_kmh": v,
             "wheel_speed_kmh": None} for i in range(n)]


def test_haversine():
    assert rides.haversine_m(51.0, 0.0, 51.001, 0.0) == pytest.approx(111.2, abs=0.5)


def test_two_rides_split_by_gap_and_stop():
    a = pts(0, 100)                                            # ~1.1 km
    stop = [{**p, "speed_kmh": 0.0} for p in pts(200, 120, lat0=51.0099, step_deg=0)]   # 240 s still
    b = pts(500, 60, lat0=51.02)
    later = pts(10_000, 60, lat0=52.0)                         # big gap -> separate ride
    r = rides.segment(a + stop + b + later)
    assert len(r) == 3
    s = rides.summary(r[0])
    assert s["distance_km"] == pytest.approx(1.1, abs=0.05) and s["max_kmh"] == 20


def test_standing_only_is_not_a_ride():
    still = [{**p, "speed_kmh": 0.5} for p in pts(0, 200, step_deg=0)]
    assert rides.segment(still) == []


def test_wheel_speed_preferred():
    p = pts(0, 40)
    for x in p:
        x["wheel_speed_kmh"] = 30.0
    s = rides.summary(rides.segment(p)[0])
    assert s["max_kmh"] == 30 and s["wheel_speed_share_pct"] == 100


def test_demo_db_roundtrip(tmp_path):
    import subprocess, sys, os
    db = tmp_path / "d.db"
    subprocess.run([sys.executable, "tools/demo_ride.py", str(db)], check=True,
                   cwd=os.path.dirname(os.path.dirname(__file__)),
                   env={**os.environ, "PYTHONPATH": "."})
    r = rides.segment(rides.load_points(sqlite3.connect(db)))
    assert len(r) == 2
    s = rides.summary(r[0])
    assert 4 < s["distance_km"] < 6 and 30 < s["max_kmh"] <= 38      # 2 laps of ~2.5 km


def test_plausibility_flags():
    good = {"sats": 8, "hdop": 1.0}
    assert rides.plausibility({**good, "wheel_speed_kmh": 25, "speed_kmh": 24}) == "ok"
    assert rides.plausibility({**good, "wheel_speed_kmh": 40, "speed_kmh": 5}) == "spin"
    assert rides.plausibility({**good, "wheel_speed_kmh": 0, "speed_kmh": 60}) == "carried"
    assert rides.plausibility({"sats": 3, "hdop": 9, "wheel_speed_kmh": 40, "speed_kmh": 5}) is None
    assert rides.plausibility({**good, "wheel_speed_kmh": None, "speed_kmh": 5}) is None


def test_ride_summary_counts_spin_time():
    p = pts(0, 60)
    for x in p:
        x.update(sats=8, hdop=1.0, wheel_speed_kmh=20.0)
    for x in p[20:30]:
        x["wheel_speed_kmh"] = 45.0                       # 10 points = 20 s spinning
    s = rides.summary(rides.segment(p)[0])
    assert s["spin_s"] == 20 and s["carried_s"] == 0
    t = rides.track(rides.segment(p)[0])
    assert t[25][4] == "spin" and t[5][4] == "ok"
    assert s["max_kmh"] == 20                             # spinning (45) not counted as max


def test_ride_name_and_slug():
    import time
    ts = time.mktime((2026, 10, 3, 14, 39, 41, 0, 0, -1))
    name, slug = rides.ride_name(ts)
    assert name == "Tur 3 okt 2026 kl. 14:39" and slug == "2026-10-03-1439"
    s = rides.summary(rides.segment(pts(ts, 60))[0])
    assert s["name"] == name and s["slug"] == slug


def test_max_speed_ignores_gps_speed_from_a_poor_fix():
    from falcon.rides import trusted_speed
    good = {"speed_kmh": 36.0, "sats": 7, "hdop": 1.4, "wheel_speed_kmh": None}
    poor = {"speed_kmh": 44.7, "sats": 4, "hdop": 7.56, "wheel_speed_kmh": None}
    old = {"speed_kmh": 30.0}                              # stored before sats/HDOP were kept
    wheel = {"speed_kmh": 44.7, "sats": 4, "hdop": 7.56, "wheel_speed_kmh": 39.0}
    assert [trusted_speed(p) for p in (good, poor, old, wheel)] == [36.0, None, 30.0, 39.0]


def test_energy_counters_survive_an_app_restart_mid_ride():
    from falcon.rideanalysis import _unwrap
    assert _unwrap([10.0, 12.0, 18.5, 0.0, 0.4, None, 2.0]) == [10.0, 12.0, 18.5, 18.5, 18.9, None, 20.5]
    assert _unwrap([1.0, 2.0, 3.0]) == [1.0, 2.0, 3.0] and _unwrap([None, None]) == [None, None]


# ---------- joining ride parts ----------
def _leg(t0, lat0, n=40, step_s=2.0, kmh=20.0, lon=0.0):
    """A straight leg northwards at a steady speed."""
    dlat = kmh / 3.6 * step_s / 111_320.0
    return [{"ts": t0 + i * step_s, "lat": lat0 + i * dlat, "lon": lon, "speed_kmh": kmh,
             "wheel_speed_kmh": kmh, "sats": 9, "hdop": 1.0} for i in range(n)]


def _three_parts():
    from falcon import rides
    a = _leg(0, 51.0)                                   # 80 s
    b = _leg(a[-1]["ts"] + 8 * 60, a[-1]["lat"])        # 8 min break at the same place
    c = _leg(b[-1]["ts"] + 30 * 60, b[-1]["lat"] + 0.03)   # 30 min without points, 3.3 km further on
    pts = a + b + c
    segs = rides.segment(pts)
    assert len(segs) == 3
    return rides, pts, segs


def test_merge_suggestion_names_breaks_and_gaps():
    rides, pts, segs = _three_parts()
    sug = rides.suggest_merges(segs)
    assert len(sug) == 1 and sug[0]["parts"] == 3 and sug[0]["ids"] == [int(s[0]["ts"]) for s in segs]
    kinds = [k["kind"] for k in sug[0]["links"]]
    assert kinds == ["paus", "lucka"] and sug[0]["confidence"] == "medel"
    assert "paus 8 min" in sug[0]["text"] and "utan positioner" in sug[0]["text"]


def test_no_suggestion_across_a_long_break_or_an_impossible_jump():
    rides = __import__("falcon.rides", fromlist=["x"])
    a = _leg(0, 51.0)
    late = _leg(a[-1]["ts"] + 2 * 3600, a[-1]["lat"])            # two hours later
    far = _leg(a[-1]["ts"] + 5 * 60, a[-1]["lat"] + 0.2)         # 22 km away after 5 minutes
    assert rides.suggest_merges(rides.segment(a + late)) == []
    assert rides.suggest_merges(rides.segment(a + far)) == []


def test_merge_joins_parts_and_can_be_undone():
    rides, pts, segs = _three_parts()
    spans = rides.add_span([], segs[0][0]["ts"] - 0.5, segs[1][-1]["ts"] + 0.5)
    m = rides.merged(segs, pts, spans)
    assert len(m) == 2 and int(m[0][0]["ts"]) == int(segs[0][0]["ts"])
    s = rides.summary(m[0])
    one = rides.summary(segs[0])["distance_km"] + rides.summary(segs[1])["distance_km"]
    assert s["distance_km"] == pytest.approx(one, abs=0.05)        # same place: the break adds nothing
    assert s["moving_s"] == rides.summary(segs[0])["moving_s"] + rides.summary(segs[1])["moving_s"]
    assert s["duration_s"] > 8 * 60                                # the break is part of the ride's time
    assert rides.merged(segs, pts, []) == segs                     # no spans = the automatic parts
    assert rides.add_span(spans, segs[1][0]["ts"], segs[2][-1]["ts"]) == [[spans[0][0], segs[2][-1]["ts"]]]


def test_a_break_of_an_hour_and_a_half_is_still_the_same_ride():
    from falcon import rides
    a = _leg(0, 51.0)
    b = _leg(a[-1]["ts"] + 85 * 60, a[-1]["lat"])                # lunch: 85 minutes at the same place
    sug = rides.suggest_merges(rides.segment(a + b))
    assert len(sug) == 1 and sug[0]["parts"] == 2 and sug[0]["confidence"] == "medel"
    assert "paus 85 min" in sug[0]["text"]
