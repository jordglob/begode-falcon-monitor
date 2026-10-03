"""Elevation sources, SWEREF 99 TM, climbs, and the energy-vs-elevation analysis."""
import math
import struct

import pytest

from falcon import rideanalysis, settings
from falcon.elevation import Dem, HgtTile, gps_altitudes, hysteresis_climbs, to_sweref99tm


def test_sweref99tm_matches_pyproj_reference():
    # values computed with pyproj (EPSG:4326 -> EPSG:3006)
    n, e = to_sweref99tm(59.32747, 18.05429)
    assert n == pytest.approx(6580501.933, abs=0.01) and e == pytest.approx(673767.491, abs=0.01)
    n, e = to_sweref99tm(67.85, 20.2)
    assert n == pytest.approx(7535335.579, abs=0.01) and e == pytest.approx(718576.385, abs=0.01)


def test_hgt_tile_bilinear(tmp_path):
    n = 11                                   # tiny 11×11 tile, elevation = row*10 + col
    data = b"".join(struct.pack(">h", r * 10 + c) for r in range(n) for c in range(n))
    p = tmp_path / "N51E000.hgt"
    p.write_bytes(data)
    t = HgtTile(p)
    assert t.value(52.0, 0.0) == 0                     # north-west corner = row 0
    assert t.value(51.0, 1.0) == 110                   # south-east corner
    assert t.value(51.95, 0.05) == pytest.approx(5 + 0.5, abs=1e-6)   # between four posts
    d = Dem(tmp_path)
    assert d.value(51.5, 0.5) == pytest.approx(55, abs=1e-6) and d.value(60, 10) is None


def test_geotiff_sweref_tile(tmp_path):
    np = pytest.importorskip("numpy")
    tifffile = pytest.importorskip("tifffile")
    # 100 × 100 m tile at 1 m around a point, elevation = northing offset
    n0, e0 = to_sweref99tm(59.30, 18.10)
    top, left = math.floor(n0) + 50, math.floor(e0) - 50
    arr = np.tile(np.arange(100, 0, -1, dtype=np.float32)[:, None], (1, 100))   # row 0 = top = 100
    p = tmp_path / "lm_test.tif"
    tifffile.imwrite(p, arr, extratags=[(33550, 12, 3, (1.0, 1.0, 0.0)),
                                       (33922, 12, 6, (0.0, 0.0, 0.0, float(left), float(top), 0.0))])
    d = Dem(tmp_path)
    assert d.tiles and "SWEREF" in d.tiles[0].name
    v = d.value(59.30, 18.10)
    assert v == pytest.approx(100 - (top - n0) + 0.5, abs=0.6)


def test_gps_altitudes_drop_bad_fixes_and_filter():
    pts = [{"alt_m": 10.0, "sats": 8, "hdop": 1.0} for _ in range(9)]
    pts[4] = {"alt_m": 90.0, "sats": 3, "hdop": 9.0}       # bad fix spike
    assert max(gps_altitudes(pts)) == 10.0


def test_hysteresis_clean_hill():
    d = [i * 10.0 for i in range(200)]
    e = [min(i, 100) * 0.5 if i < 100 else 50 - (i - 100) * 0.5 for i in range(200)]
    c = hysteresis_climbs(d, e, 2.0)
    assert [x["dir"] for x in c] == ["up", "down"] and c[0]["dh_m"] == 50.0


def _ride(vfun, noise=0.0):
    import random
    r = random.Random(1)
    pts, wo, wr, t = [], 0.0, 0.0, 1e9
    for i in range(900):
        dd = i * 10.0
        h = 20 * math.sin(dd / 700)
        vm = vfun(i)
        g = (20 * math.sin((dd + 10) / 700) - h) / 10
        p = 60 + 18 * vm + 0.35 * vm ** 3 + (120 * 9.81 * vm * g / 0.8 if g > 0 else 120 * 9.81 * vm * g * 0.6)
        dt = 10 / vm
        if p >= 0:
            wo += p * dt / 3600
        else:
            wr += -p * dt / 3600
        pts.append({"ts": t, "lat": 51.0 + dd / 111195, "lon": 0.0, "alt_m": h + r.gauss(0, noise),
                    "sats": 9, "hdop": 0.8, "speed_kmh": vm * 3.6, "wheel_speed_kmh": vm * 3.6,
                    "wh_out_cum": wo, "wh_regen_cum": wr, "pwm_max": 30 + 100 * max(g, 0)})
        t += dt
    return pts


def test_analysis_recovers_physics():
    a = rideanalysis.analyze(_ride(lambda i: 5 + 3 * math.sin(i / 37)), settings.defaults(), None, 120)
    reg = a["regression"]
    assert reg["up_wh_per_m"] == pytest.approx(120 * 9.81 / 3600 / 0.8, rel=0.05)     # 0.409
    assert reg["down_wh_per_m"] == pytest.approx(120 * 9.81 / 3600 * 0.6, rel=0.1)    # 0.196
    assert a["physics"]["climb_efficiency_regression_pct"] == pytest.approx(80, abs=4)
    assert a["totals"]["ascent_m"] == pytest.approx(83, abs=5)
    assert a["climbs"] and all(c["wh_net"] is not None for c in a["climbs"])
    up = [c for c in a["climbs"] if c["dir"] == "up"]
    assert up[0]["min_margin_pct"] is not None and up[0]["wh_per_m"] > 0.3


def test_analysis_without_energy_still_gives_elevation():
    pts = _ride(lambda i: 6.0)
    for p in pts:
        p["wh_out_cum"] = p["wh_regen_cum"] = None
    a = rideanalysis.analyze(pts, settings.defaults(), None, None)
    assert a["ok"] and a["totals"]["ascent_m"] > 70 and a["totals"]["wh_out"] is None
    assert a["totals"]["energy_coverage_pct"] == 0


def test_grade_energy_bins():
    a = rideanalysis.analyze(_ride(lambda i: 6.0), settings.defaults(), None, 120)
    bins = rideanalysis.grade_energy_bins([a])
    up = [b for b in bins if b["grade_pct"] > 1 and b["wh_per_km"]]
    down = [b for b in bins if b["grade_pct"] < -1 and b["wh_per_km"] is not None]
    assert up and down and max(b["wh_per_km"] for b in up) > min(b["wh_per_km"] for b in down)


def test_settings_validation_and_mass():
    v = settings.validate({"rider_kg": "82,5", "wheel_kg": "35", "gear_kg": ""})
    assert v["rider_kg"] == 82.5 and v["gear_kg"] is None
    assert settings.total_mass({**settings.defaults(), **v}) == 117.5
    with pytest.raises(ValueError):
        settings.validate({"rider_kg": 500})
