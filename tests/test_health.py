"""Batterihälsa: simulated charges, rides out of range, parking, current steps."""
import sqlite3

import pytest

from falcon.health import Health, wheel_percent, REST_S

KV = "CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);"


def mk_db():
    db = sqlite3.connect(":memory:")
    db.executescript(KV)
    return db


def snap(cell_v, amps=0.0, charging=False, odo_m=836000, cell_offsets=None, temp=25):
    mv = [round(cell_v * 1000)] * 48
    for i, d in (cell_offsets or {}).items():
        mv[i] += d
    act = "laddning" if charging else "urladdning"
    groups = {k: {"group": k, "bms": 1 if k < 2 else 2, "half": 1 + k % 2, "activity": act,
                  "current_a": amps / 2, "voltage_v": round(cell_v * 24, 1)} for k in range(4)}
    return {
        "groups": groups,
        "bms": {n: {"current_a": amps / 2, "activity": act, "temp_max_c": temp} for n in (1, 2)},
        "battery_current_a": -amps if charging else amps,
        "cells": {"A": {"cells_mv": mv[:24]}, "B": {"cells_mv": mv[24:]}},
        "p0": {"voltage_raw": round(cell_v * 16 * 100)},
        "p4": {"odometer_raw": odo_m},
        "p7": {"battery_current_a": -amps if charging else amps},
    }


def run(h, t0, secs, **kw):
    for t in range(secs):
        h.tick(t0 + t, snap(**kw), True)
    return t0 + secs


def test_wheel_percent_formula():
    assert wheel_percent(4800) == 0 and wheel_percent(6550) == 100
    assert wheel_percent(6466) == 95


def test_charge_gives_capacity_point():
    h = Health(mk_db())
    t = run(h, 0, REST_S + 10, cell_v=3.80)                 # rest at 50 %
    # charge 10 A for 2 h = 20 Ah, ending at 90 %
    for i in range(7200):
        v = 3.80 + 0.30 * i / 7200
        h.tick(t + i, snap(v + 0.03, amps=10.0, charging=True), True)
    t += 7200
    t = run(h, t, 900, cell_v=4.10)                           # rest at 90 %
    r = h.report()
    cap = r["capacity"]["points"]
    assert len(cap) == 1 and cap[0]["method"] == "charge"
    assert cap[0]["ah"] == pytest.approx(20 / 0.40, rel=0.02)  # 50 Ah
    assert r["counters"]["ah_in"] == pytest.approx(20, rel=0.01)
    assert r["capacity"]["health_pct"] == 100.0


def test_charger_flaps_counted():
    h = Health(mk_db())
    t = run(h, 0, REST_S + 10, cell_v=3.80)
    for k in range(3):                                         # 3 drop-outs of 20 s
        t = run(h, t, 300, cell_v=3.9, amps=8.0, charging=True)
        t = run(h, t, 20, cell_v=3.9)
    t = run(h, t, 300, cell_v=3.9, amps=8.0, charging=True)
    t = run(h, t, 1000, cell_v=3.95)
    s = [x for x in h.report()["sessions"] if x["kind"] == "charge"]
    assert s and s[0]["interruptions"] == 3


def test_ride_out_of_range_reconstructed():
    h = Health(mk_db())
    t = run(h, 0, REST_S + 10, cell_v=4.10, odo_m=836000)      # 90 % at home
    h.tick(t, None, False)                                     # rides away
    t += 3600
    t = run(h, t, REST_S + 10, cell_v=3.80, odo_m=856000)      # back, 20 km, 50 %
    s = [x for x in h.report()["sessions"] if x["kind"] == "ride_gap"]
    assert s and s[0]["km"] == pytest.approx(20) and s[0]["soc_start"] == 90 and s[0]["soc_end"] == 50


def test_ride_then_immediate_charge_keeps_ride():
    h = Health(mk_db())
    t = run(h, 0, REST_S + 10, cell_v=4.10, odo_m=836000)
    h.tick(t, None, False)
    t += 3600
    t = run(h, t, 30, cell_v=3.80, odo_m=850000)               # back, plugs in at once
    t = run(h, t, 600, cell_v=3.85, amps=8.0, charging=True, odo_m=850000)
    s = [x for x in h.report()["sessions"] if x["kind"] == "ride_gap"]
    assert s and s[0]["km"] == pytest.approx(14)


def test_self_discharge_finds_leaking_cell():
    h = Health(mk_db())
    t = run(h, 0, REST_S + 10, cell_v=4.00)
    h.tick(t, None, False)                                     # off for 48 h
    t += 48 * 3600
    t = run(h, t, REST_S + 10, cell_v=3.996, cell_offsets={30: -40})
    sd = h.report()["self_discharge"]
    assert sd and sd[0]["worst_cell"] == "B7"
    assert sd[0]["worst_mv_day"] == pytest.approx(22, abs=1)


def test_cell_resistance_from_step():
    h = Health(mk_db())
    t = run(h, 0, 30, cell_v=4.0)
    # 20 A step: string current 10 A; cell A5 has 6 mΩ (60 mV drop), others 3 mΩ
    off = {i: -30 for i in range(48)}
    off[4] = -60
    t = run(h, t, 30, cell_v=4.0, amps=20.0, cell_offsets=off)
    ir = h.report()["cell_ir_mohm"]
    assert ir["A5"] == pytest.approx(6.0, rel=0.05) and ir["B1"] == pytest.approx(3.0, rel=0.05)
    assert h.report()["cell_ir_flag"][0] == "A5"


def test_exposure_counts_high_soc_time():
    h = Health(mk_db())
    run(h, 1_700_000_000 - 40_000, 3601, cell_v=4.15, temp=41)   # local midday, no midnight
    e = h.report()["exposure"][0]
    assert e["s_soc95"] == pytest.approx(3600, abs=5) and e["s_t40"] == pytest.approx(3600, abs=5)


def test_counters_persist():
    db = mk_db()
    h = Health(db)
    run(h, 0, 3600, cell_v=3.9, amps=10.0)
    h.save()
    assert Health(db).ah_out == pytest.approx(10.0, rel=0.01)
