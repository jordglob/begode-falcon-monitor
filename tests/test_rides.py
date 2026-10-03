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
